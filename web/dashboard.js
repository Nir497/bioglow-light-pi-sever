'use strict';
const names = {'config-settings':'Settings & backup','wled-log':'WLED log','distance-test':'Distance test',overview:'Overview',radar:'Radar',light:'Lux sensor',lighting:'WLED light',direction:'Direction',birdcast:'BirdCast',recordings:'Recordings','config-direction':'Direction','config-directionality':'Direction','config-schedule':'Recording schedule','config-lighting':'WLED hardware','logs-sensors':'Sensor histories'};
const sectionMap = {'config-settings':'Configuration',light:'Input',radar:'Input',recordings:'Input',birdcast:'Input',lighting:'Output',direction:'Output','distance-test':'Output','config-direction':'Configuration','config-directionality':'Configuration','config-schedule':'Configuration','config-lighting':'Configuration','wled-log':'Logs','logs-sensors':'Logs'};
const itemNames = {'config-settings':'Settings & backup',light:'Lux sensor',radar:'Radar',recordings:'Recordings',birdcast:'BirdCast',lighting:'WLED light',direction:'Direction','distance-test':'Distance test',overview:'Station overview','config-direction':'Direction','config-directionality':'Direction','config-schedule':'Recording schedule','config-lighting':'WLED hardware','wled-log':'WLED log','logs-sensors':'Sensor histories'};
const titles = {'config-settings':'Settings & backup','wled-log':'WLED command log','distance-test':'Distance sensor · LED test',overview:'Station overview',radar:'Presence & movement',light:'Luminance',lighting:'WLED strip lighting',direction:'Lamp beam direction',birdcast:'Migration outlook',recordings:'Sound library','config-direction':'Direction configuration','config-directionality':'Direction configuration','config-schedule':'Audio collection schedule','config-lighting':'WLED hardware settings','logs-sensors':'Sensor history logs'};
const descriptions = {'config-settings':'Automatic saving and portable configuration backups.','wled-log':'USB traffic observed by this server · Latest 2,000 events · Cleared on server restart.','distance-test':'Live radar readings and single-LED output, refreshed every half second. The test continues when you leave this page; select Off to stop it.',overview:'Your sensors, migration data, and sound — in one place.',radar:'Live LD2410C presence & activity estimator, radial distance gates (0–6.75 m), and raw UART diagnostics.',light:'Measure the light around your station with the VEML7700.',lighting:'Control your 3.2 ft strip through the SP803E WLED-ESP32 USB controller.',direction:'Overhead aerial view to steer the vertical light beam in any direction around the lamp.',birdcast:'Fetch the latest migration snapshot for a US state.',recordings:'Capture a moment. Listen again. Manage your saved recordings.','config-direction':'Configure spiral wrap spacing and beam width to align the vertical column of light.','config-directionality':'Configure spiral wrap spacing and beam width to align the vertical column of light.','config-schedule':'Automate microphone sampling every two minutes across all clients.','config-lighting':'Hardware parameters, serial connection details, and output bounds for WLED.','logs-sensors':'Historical readings collected from presence, light, and migration sensors.'};
let sources = {}, recordings = [], regions = [], region = 'US-CA', loadingLibrary = true, libraryError = '', cacheError = '', stationMode = 'normal', directionAlignment = 144, directionBeamWidth = 1, directionOffset = 0, systemServices = {};
let browserPersistenceError = '', configurationError = '';
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
const view = () => {
  const hash = location.hash.slice(1);
  if (hash === 'config-directionality') return 'config-direction';
  return Object.hasOwn(names, hash) ? hash : location.pathname === '/distance-test' ? 'distance-test' : 'overview';
};
function notice(message, error = false) { if (browserPersistenceError) { message = [message,browserPersistenceError].filter(Boolean).join(' '); error = true; } const box = document.querySelector('#notice'); box.textContent = message; box.classList.toggle('error',error); box.hidden = !message; }
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
  return facts([['Connection','Pi server → USB serial → SP803E'],['USB port',escapeHtml(device.serial_port || 'Not selected yet')],['Serial speed',`${number(device.baudrate ?? 115200)} baud`],['Strip length','192 pixels · 3½ spiral wraps'],['WLED pixels',device.led_count == null ? 'Awaiting controller' : number(device.led_count)],['Last confirmed coverage',device.covered_leds == null ? 'Not verified yet' : `${number(device.covered_leds)} / ${number(device.expected_led_count ?? 192)} pixels`],['Confirmed color',escapeHtml(device.confirmed_color || 'Not verified yet')],['Confirmed brightness',device.confirmed_brightness == null ? 'Not verified yet' : device.confirmed_on === false ? 'Off' : `${Math.round(device.confirmed_brightness / 255 * 100)}% (${number(device.confirmed_brightness)} / 255)`],['Segment brightness',device.segment_brightness == null ? 'Not verified yet' : `${number(device.segment_brightness)} / 255`],['WLED current limit',device.current_limit_ma == null ? 'Not reported' : device.current_limit_ma === 0 ? 'Disabled in WLED' : `${number(device.current_limit_ma)} mA`],['Firmware',escapeHtml(device.firmware || 'Awaiting controller')]]) + '<p class="timestamp">Normal modes cover pixels 1–192 with a solid color and no spacing; Distance test lights one pixel. WLED must be configured for at least 192 pixels; a shorter configured output is reported as an error.</p>';
}
function lightingLuxReading() {
  if (lighting.lux == null) return 'Unavailable';
  const stale = lighting.lux_age_seconds > (lighting.lux_stale_after_seconds ?? 3);
  return `${lighting.lux.toLocaleString(undefined,{maximumFractionDigits:2})} lux${stale ? ' (stale)' : ''}`;
}
function lightingLuxRule() {
  const dark = lighting.lux_dark_threshold ?? 15;
  const light = lighting.lux_light_threshold ?? 25;
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
  return `${reason} · Ambient ${lighting.ambient_state || 'unknown'} · Lux ${lighting.lux == null ? 'unavailable' : lighting.lux.toLocaleString(undefined,{maximumFractionDigits:2})} · Lux age ${lighting.lux_age_seconds == null ? 'unavailable' : lighting.lux_age_seconds.toFixed(1) + ' s'} · Filtered dark <${lighting.lux_dark_threshold ?? 15} lux / Light >${lighting.lux_light_threshold ?? 25} lux · Stale after ${lighting.lux_stale_after_seconds ?? 3} s`;
}
function lightingSummary() {
  const base = `${lighting.mode || 'auto'} · ${lighting.status || 'waiting'}`;
  if (lighting.mode === 'auto') return `${base} · ${lighting.lighting_state || 'waiting'} · Radar target ${lighting.presence_detected ? 'present' : 'absent/unavailable'} · Current target distance ${lighting.person_distance_m == null ? '—' : Number(lighting.person_distance_m).toFixed(2) + ' m'} · Distance target ${lighting.distance_target_percent ?? '—'}% · Migration ${lighting.migration_level || 'unknown'} · Cap ${lighting.migration_cap_percent ?? 10}% · Final target ${lighting.final_target_percent ?? 0}% · Smoothed output ${lighting.output_percent ?? 0}% · Acknowledged ${lighting.actual_brightness_percent == null ? 'unknown' : lighting.actual_brightness_percent + '%'} (physical output unmeasured) · ${lighting.migration_error || ''} · Watchdog ${lighting.controller_watchdog_status || 'unconfirmed'} · Sample ${lighting.sample_age_seconds == null ? 'waiting' : lighting.sample_age_seconds.toFixed(1) + ' s ago'} · Ack ${lighting.ack_age_seconds == null ? 'waiting' : lighting.ack_age_seconds.toFixed(1) + ' s ago'}`;
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
  const color = /^#[0-9a-f]{6}$/i.test(lighting.color || '') ? lighting.color : '#ffd6aa';
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
      <label for="wled-color-picker">LED color</label>
      <input id="wled-color-picker" name="color-picker" type="color" value="${color}">
      <label for="wled-color">Hex code</label>
      <input id="wled-color" name="color" type="text" value="${color}" pattern="#[0-9a-fA-F]{6}" maxlength="7" placeholder="#FFD6AA" title="Enter # followed by six hexadecimal digits" required spellcheck="false">
      <button class="button secondary" type="submit">Apply color &amp; brightness</button>
      </fieldset>
    </form>
    <p class="timestamp">Distance test: LED 1 at 1 m or closer; LED 192 at 6 m or farther. Between 1–6 m, the dot moves along LEDs 2–191 at 25% amber. No target turns lights off; the radar’s own absence delay still applies. Stale readings turn lights off and show an error.</p>
    <p class="timestamp">Auto uses filtered ambient light with configurable 15/25 lux hysteresis. Daylight forces immediate OFF. Invalid or stale lux keeps Auto off until darkness is confirmed. Manual On and Distance test bypass this rule.</p>
    <p class="timestamp" id="wled-manual-help">Apply saves manual settings. If the light is off, it stays off. On restores your chosen brightness. Auto increases brightness as the nearest reported radar target approaches: 60% at 0 m to 10% at 5 m, capped by migration (unknown: 10%). Stationary radar presence stays active. Presence loss holds the last permitted output for 10 seconds, then fades off. Invalid radar requests immediate OFF. Targets beyond 5 m cannot activate lighting. Radar does not identify humans. These are prototype settings, not verified wildlife-safe limits. Auto also works with stock WLED; the optional watchdog module turns output off after Pi communication stops. Color changes preserve Auto.</p>
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
  const picker = form.querySelector('[name="color-picker"]');
  if (picker && lighting.color) picker.value = lighting.color;
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
      saveBrowserPreferences();
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
function stationServicesCards() {
  const bg = systemServices.bioglow_service || {};
  const cf = systemServices.cloudflare_tunnel || {};

  const bgBadge = bg.started
    ? '<span class="badge">Running</span>'
    : (bg.installed ? '<span class="badge unavailable">Stopped</span>' : '<span class="badge empty">Not installed</span>');

  const cfBadge = cf.started
    ? '<span class="badge">Active & connected</span>'
    : (cf.set_up ? '<span class="badge unavailable">Configured (stopped)</span>'
      : (cf.installed ? '<span class="badge unavailable">Token missing</span>'
        : '<span class="badge empty">Not set up</span>'));

  const bgFacts = facts([
    ['Service installed', bg.installed ? 'Yes (bioglow.service)' : 'No (not installed)'],
    ['Auto-start on boot', bg.enabled ? 'Enabled' : (bg.installed ? 'Disabled' : 'Not installed')],
    ['Service status', bg.started ? 'Active (running)' : (bg.installed ? 'Inactive (stopped)' : 'Not started')],
    ['Install command', '<code>sudo ./scripts/install_service.sh</code>']
  ]);

  const cfFacts = facts([
    ['Tunnel connector', cf.installed ? 'Installed (cloudflared)' : 'Not installed'],
    ['Tunnel token', cf.token_configured ? 'Configured (tunnel-token)' : 'Not configured'],
    ['Tunnel status', cf.started ? 'Active (connected)' : (cf.set_up ? 'Inactive (stopped)' : 'Not running')],
    ['Public access', cf.started ? '<a href="http://glow.ultraviolet.nirtech.org" target="_blank" rel="noopener noreferrer">glow.ultraviolet.nirtech.org ↗</a>' : 'See CLOUDFLARE.md']
  ]);

  return `
    <article class="card" data-service-card="bioglow">
      <div class="card-header">
        <div class="source-label">
          <div class="source-icon" aria-hidden="true">⚡</div>
          <div>
            <h2>BioGlow service</h2>
            <small>systemd · bioglow.service</small>
          </div>
        </div>
        ${bgBadge}
      </div>
      ${bgFacts}
      <p class="timestamp">${bg.installed ? (bg.started ? 'The server is running as a systemd boot service.' : 'Service unit exists but is stopped. Run: sudo systemctl start bioglow') : 'Start the server automatically whenever the Pi boots with the install script.'}</p>
    </article>
    <article class="card" data-service-card="cloudflare">
      <div class="card-header">
        <div class="source-label">
          <div class="source-icon" aria-hidden="true">☁</div>
          <div>
            <h2>Cloudflare tunnel</h2>
            <small>cloudflared · remote access</small>
          </div>
        </div>
        ${cfBadge}
      </div>
      ${cfFacts}
      <p class="timestamp">${cf.set_up ? (cf.started ? 'Remote tunnel is running and forwarding public traffic to port 8000.' : 'Tunnel is configured but connector is stopped. Run: sudo systemctl start cloudflared') : 'Enables public dashboard access without open router ports. Follow CLOUDFLARE.md to configure.'}</p>
    </article>
  `;
}
function updateServicesSection() {
  const container = content.querySelector('[data-station-services]');
  if (container) {
    container.innerHTML = stationServicesCards();
  }
}
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
      if (latest.services) {
        systemServices = latest.services;
        if (view() === 'overview') updateServicesSection();
      }
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
  const dirCard = document.querySelector('.direction-controls-card');
  if (dirCard) {
    dirCard.classList.toggle('controls-disabled', stationMode !== 'direction_config');
    const noticeEl = dirCard.querySelector('.direction-disabled-notice');
    if (noticeEl) noticeEl.hidden = stationMode === 'direction_config';
    const slider = dirCard.querySelector('#direction-alignment-slider');
    if (slider) slider.disabled = stationMode !== 'direction_config';
    dirCard.querySelectorAll('[data-preset-alignment]').forEach(btn => {
      btn.disabled = stationMode !== 'direction_config';
    });
    const bwSlider = dirCard.querySelector('#direction-beam-width-slider');
    if (bwSlider) bwSlider.disabled = stationMode !== 'direction_config';
    const maxBw = maxBeamWidthForAlignment(directionAlignment || 144);
    dirCard.querySelectorAll('[data-preset-beam-width]').forEach(btn => {
      const targetBw = parseInt(btn.dataset.presetBeamWidth, 10);
      btn.disabled = stationMode !== 'direction_config' || targetBw > maxBw;
    });
  }
  const outDirCard = document.querySelector('.direction-output-card');
  if (outDirCard) {
    outDirCard.classList.toggle('controls-disabled', stationMode !== 'direction_config');
    const noticeEl = outDirCard.querySelector('.direction-disabled-notice');
    if (noticeEl) noticeEl.hidden = stationMode === 'direction_config';
    outDirCard.querySelectorAll('[data-preset-direction], [data-nudge-direction]').forEach(btn => {
      btn.disabled = stationMode !== 'direction_config';
    });
  }
}
function maxBeamWidthForAlignment(n) {
  const spacing = parseInt(n || 144, 10);
  return Math.min(143, 2 * Math.floor(spacing / 2) + 1);
}
let alignmentDebounceTimer = null;
let directionDebounceTimer = null;
function updateAlignmentDisplay(val) {
  const n = parseInt(val, 10);
  const count = Math.floor((192 - 1) / n) + 1;
  const litText = count === 1 ? '1 LED lit (LED #1)' : `${count} of 192 LEDs lit`;
  const output = document.querySelector('#direction-alignment-value');
  if (output) output.textContent = `n = ${n} (${litText})`;
  const nextEl = document.querySelector('#alignment-next-led');
  if (nextEl) nextEl.textContent = n >= 192 ? 'None (beyond LED 192)' : `LED #${1 + n}`;
  const countEl = document.querySelector('#alignment-lit-count');
  if (countEl) countEl.textContent = `${count} / 192`;
}
function updateBeamWidthDisplay(bwVal, nVal) {
  const bw = parseInt(bwVal, 10);
  const n = parseInt(nVal || directionAlignment || 144, 10);
  const maxBw = maxBeamWidthForAlignment(n);
  const radius = Math.floor((bw - 1) / 2);
  const desc = bw === 1 ? '1 pixel (original LED only)' : `${bw} pixels (±${radius} on each side plus original LED)`;
  const output = document.querySelector('#direction-beam-width-value');
  if (output) output.textContent = `Width = ${bw} (${desc})`;
  const radiusEl = document.querySelector('#beam-width-radius');
  if (radiusEl) radiusEl.textContent = radius === 0 ? 'None (0 on each side)' : `±${radius} LEDs on each side`;
  const totalEl = document.querySelector('#beam-width-total');
  if (totalEl) totalEl.textContent = `${bw} LEDs per anchor`;
  const maxEl = document.querySelector('#beam-width-max');
  if (maxEl) maxEl.textContent = `${maxBw} LEDs (max for n=${n})`;
  const tickMax = document.querySelector('#beam-width-tick-max');
  if (tickMax) tickMax.textContent = `Max: ${maxBw} pixels`;
}
async function sendAlignment(n, bw) {
  const alignmentVal = parseInt(n || directionAlignment || 144, 10);
  const beamWidthVal = parseInt(bw || directionBeamWidth || 1, 10);
  const offsetVal = directionOffset % alignmentVal;
  try {
    const res = await api('/v1/direction/alignment', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({alignment: alignmentVal, beam_width: beamWidthVal, offset: offsetVal})
    });
    if (res.alignment) directionAlignment = res.alignment;
    if (res.beam_width) directionBeamWidth = res.beam_width;
    if (res.offset != null) directionOffset = res.offset;
    if (res.lighting?.status==='controller_unavailable') notice('Direction preferences saved; controller delivery failed.',true);
  } catch (err) {
    notice(`Could not set direction parameters: ${err.message}`, true);
  }
}
async function sendDirection(offset) {
  const alignmentVal = parseInt(directionAlignment || 144, 10);
  const beamWidthVal = parseInt(directionBeamWidth || 1, 10);
  const offsetVal = ((parseInt(offset, 10) % alignmentVal) + alignmentVal) % alignmentVal;
  directionOffset = offsetVal;
  try {
    const res = await api('/v1/direction', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        alignment: alignmentVal,
        beam_width: beamWidthVal,
        offset: offsetVal
      })
    });
    if (res.offset != null) directionOffset = res.offset;
    if (res.alignment) directionAlignment = res.alignment;
    if (res.beam_width) directionBeamWidth = res.beam_width;
    if (res.lighting?.status==='controller_unavailable') notice('Direction preferences saved; controller delivery failed.',true);
  } catch (err) {
    notice(`Could not set direction: ${err.message}`, true);
  }
}
function getDialGeometry(offset, alignment, beamWidth) {
  const n = parseInt(alignment || directionAlignment || 144, 10);
  const w = parseInt(beamWidth || directionBeamWidth || 1, 10);
  const s = ((parseInt(offset || 0, 10) % n) + n) % n;
  const angleDeg = (s / n) * 360;
  const spanDeg = Math.min(360, Math.max(360 / n, (w / n) * 360));
  const cx = 160, cy = 160, R = 108;
  const ac = angleDeg * Math.PI / 180;
  const a1 = (angleDeg - spanDeg / 2) * Math.PI / 180;
  const a2 = (angleDeg + spanDeg / 2) * Math.PI / 180;
  const x1 = cx + R * Math.sin(a1);
  const y1 = cy - R * Math.cos(a1);
  const x2 = cx + R * Math.sin(a2);
  const y2 = cy - R * Math.cos(a2);
  const largeArc = spanDeg > 180 ? 1 : 0;
  const wedgePath = spanDeg >= 359.9
    ? `M ${cx} ${cy - R} A ${R} ${R} 0 1 1 ${cx} ${cy + R} A ${R} ${R} 0 1 1 ${cx} ${cy - R} Z`
    : `M ${cx} ${cy} L ${x1.toFixed(1)} ${y1.toFixed(1)} A ${R} ${R} 0 ${largeArc} 1 ${x2.toFixed(1)} ${y2.toFixed(1)} Z`;
  const arcPath = spanDeg >= 359.9
    ? `M ${cx} ${cy - R} A ${R} ${R} 0 1 1 ${cx} ${cy + R} A ${R} ${R} 0 1 1 ${cx} ${cy - R}`
    : `M ${x1.toFixed(1)} ${y1.toFixed(1)} A ${R} ${R} 0 ${largeArc} 1 ${x2.toFixed(1)} ${y2.toFixed(1)}`;
  const nx = cx + (R - 6) * Math.sin(ac);
  const ny = cy - (R - 6) * Math.cos(ac);
  const hx = cx + R * Math.sin(ac);
  const hy = cy - R * Math.cos(ac);

  const anchors = [];
  for (let a = 1 + s; a <= 192; a += n) {
    anchors.push(a);
  }

  let cardinal = `${angleDeg.toFixed(1)}°`;
  if (Math.abs(angleDeg - 0) < 1e-2 || Math.abs(angleDeg - 360) < 1e-2) cardinal = '0° (Front · LED #1)';
  else if (Math.abs(angleDeg - 90) < 1e-2) cardinal = '90° (Right)';
  else if (Math.abs(angleDeg - 180) < 1e-2) cardinal = '180° (Back)';
  else if (Math.abs(angleDeg - 270) < 1e-2) cardinal = '270° (Left)';

  return {
    n, w, s, angleDeg, spanDeg, cx, cy, R,
    wedgePath, arcPath, nx, ny, hx, hy,
    anchors, cardinal
  };
}
function updateDialVisuals(offset) {
  const g = getDialGeometry(offset, directionAlignment, directionBeamWidth);
  const wedge = document.querySelector('#dial-beam-wedge');
  if (wedge) wedge.setAttribute('d', g.wedgePath);
  const arc = document.querySelector('#dial-beam-arc');
  if (arc) arc.setAttribute('d', g.arcPath);
  const needle = document.querySelector('#dial-needle');
  if (needle) {
    needle.setAttribute('x2', g.nx.toFixed(1));
    needle.setAttribute('y2', g.ny.toFixed(1));
  }
  const handle = document.querySelector('#dial-handle');
  if (handle) {
    handle.setAttribute('cx', g.hx.toFixed(1));
    handle.setAttribute('cy', g.hy.toFixed(1));
  }
  const angleVal = document.querySelector('#direction-angle-value');
  if (angleVal) angleVal.textContent = g.cardinal;
  const offsetVal = document.querySelector('#direction-offset-value');
  if (offsetVal) offsetVal.textContent = `+${g.s} LEDs`;
  const anchorsVal = document.querySelector('#direction-anchors-value');
  if (anchorsVal) anchorsVal.textContent = g.anchors.map(a => `LED #${a}`).join(', ');
  const spanVal = document.querySelector('#direction-span-value');
  if (spanVal) spanVal.textContent = `${g.w} px (${g.spanDeg.toFixed(1)}°)`;
}
function directionOutputPage() {
  const isDirConfig = stationMode === 'direction_config';
  const g = getDialGeometry(directionOffset, directionAlignment, directionBeamWidth);

  let tickMarks = '';
  for (let d = 0; d < 360; d += 30) {
    const rad = d * Math.PI / 180;
    const isMajor = d % 90 === 0;
    const r1 = isMajor ? 138 : 142;
    const r2 = 148;
    const x1 = (160 + r1 * Math.sin(rad)).toFixed(1);
    const y1 = (160 - r1 * Math.cos(rad)).toFixed(1);
    const x2 = (160 + r2 * Math.sin(rad)).toFixed(1);
    const y2 = (160 - r2 * Math.cos(rad)).toFixed(1);
    tickMarks += `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${isMajor ? '#496052' : '#8fa094'}" stroke-width="${isMajor ? 2 : 1}"/>`;
  }

  let ledDots = '';
  if (g.n <= 72) {
    for (let k = 0; k < g.n; k++) {
      const dotRad = (k / g.n) * 2 * Math.PI;
      const dx = (160 + 108 * Math.sin(dotRad)).toFixed(1);
      const dy = (160 - 108 * Math.cos(dotRad)).toFixed(1);
      const isAnchor = k === g.s;
      ledDots += `<circle cx="${dx}" cy="${dy}" r="${isAnchor ? 3.5 : 2}" fill="${isAnchor ? '#ffa028' : '#3d5246'}" stroke="${isAnchor ? '#fff' : 'none'}" stroke-width="1"/>`;
    }
  }

  return `<article class="card directionality-mode-card">` +
    `<div class="directionality-toggle-row">` +
      `<div class="directionality-toggle-info">` +
        `<h2>Direction Config Mode</h2>` +
        `<p class="timestamp">Toggle Direction Config mode on or off. When active, the station operates in direction configuration mode.</p>` +
      `</div>` +
      `<label class="switch-control" for="direction-config-toggle">` +
        `<input type="checkbox" id="direction-config-toggle" ${isDirConfig ? 'checked' : ''}>` +
        `<span class="switch-slider" aria-hidden="true"></span>` +
        `<span class="switch-label">${isDirConfig ? 'Active' : 'Inactive'}</span>` +
      `</label>` +
    `</div>` +
  `</article>` +
  `<article class="card direction-output-card ${isDirConfig ? '' : 'controls-disabled'}">` +
    `<div class="direction-disabled-notice" ${isDirConfig ? 'hidden' : ''}><span aria-hidden="true">🔒</span> Direction config mode is inactive. Activate Direction config mode above to enable direction controls.</div>` +
    `<div class="card-header">` +
      `<div>` +
        `<h2>Lamp Beam Direction</h2>` +
        `<p class="timestamp">Overhead aerial view. Click or drag anywhere on the circle to steer the vertical light beam in that direction.</p>` +
      `</div>` +
      `<span class="badge ${isDirConfig ? 'badge-recv' : 'empty'}">${isDirConfig ? 'Mode: Direction Config' : 'Mode: Normal'}</span>` +
    `</div>` +

    `<div class="lamp-dial-container">` +
      `<svg id="lamp-dial-svg" class="lamp-dial-svg" viewBox="0 0 320 320" width="320" height="320" role="img" aria-label="Aerial view of the cylindrical lamp to select direction">` +
        `<defs>` +
          `<radialGradient id="lamp-body-grad" cx="50%" cy="50%" r="50%">` +
            `<stop offset="0%" stop-color="#2a3832"/>` +
            `<stop offset="85%" stop-color="#18221d"/>` +
            `<stop offset="100%" stop-color="#0f1713"/>` +
          `</radialGradient>` +
          `<radialGradient id="beam-wedge-grad" cx="50%" cy="50%" r="50%">` +
            `<stop offset="10%" stop-color="#ffb84d" stop-opacity="0.85"/>` +
            `<stop offset="65%" stop-color="#ffa028" stop-opacity="0.45"/>` +
            `<stop offset="100%" stop-color="#ff8800" stop-opacity="0.1"/>` +
          `</radialGradient>` +
        `</defs>` +
        `<circle cx="160" cy="160" r="148" class="dial-track" fill="#f8faf7" stroke="#d5ded7" stroke-width="1.5"/>` +
        `${tickMarks}` +
        `<text x="160" y="24" class="dial-cardinal">0° · LED #1</text>` +
        `<text x="296" y="164" class="dial-cardinal">90°</text>` +
        `<text x="160" y="304" class="dial-cardinal">180°</text>` +
        `<text x="24" y="164" class="dial-cardinal">270°</text>` +
        `<circle cx="160" cy="160" r="108" fill="url(#lamp-body-grad)" stroke="#3e5348" stroke-width="2.5"/>` +
        `<circle cx="160" cy="160" r="65" fill="none" stroke="#25352c" stroke-width="1" stroke-dasharray="3,3"/>` +
        `${ledDots}` +
        `<path id="dial-beam-wedge" class="dial-beam-wedge" d="${g.wedgePath}" fill="url(#beam-wedge-grad)"/>` +
        `<path id="dial-beam-arc" class="dial-beam-arc" d="${g.arcPath}" fill="none" stroke="#ffa028" stroke-width="9" stroke-linecap="round"/>` +
        `<line id="dial-needle" x1="160" y1="160" x2="${g.nx.toFixed(1)}" y2="${g.ny.toFixed(1)}" stroke="#ffffff" stroke-width="2.5" stroke-linecap="round" opacity="0.9"/>` +
        `<circle cx="160" cy="160" r="20" fill="#18221d" stroke="#506b5c" stroke-width="2"/>` +
        `<circle cx="160" cy="160" r="5" fill="#ffa028"/>` +
        `<circle id="dial-handle" cx="${g.hx.toFixed(1)}" cy="${g.hy.toFixed(1)}" r="10" class="dial-handle" fill="#ffa028" stroke="#ffffff" stroke-width="2.5"/>` +
      `</svg>` +
    `</div>` +

    `<div class="alignment-summary-row direction-stats-grid">` +
      `<div class="alignment-stat"><span class="stat-label">Beam Direction</span><span class="stat-value" id="direction-angle-value">${g.cardinal}</span></div>` +
      `<div class="alignment-stat"><span class="stat-label">Offset along Strip</span><span class="stat-value" id="direction-offset-value">+${g.s} LEDs</span></div>` +
      `<div class="alignment-stat"><span class="stat-label">Beam Width</span><span class="stat-value" id="direction-span-value">${g.w} px (${g.spanDeg.toFixed(1)}°)</span></div>` +
      `<div class="alignment-stat"><span class="stat-label">Active Anchors</span><span class="stat-value" id="direction-anchors-value">${g.anchors.map(a=>`LED #${a}`).join(', ')}</span></div>` +
    `</div>` +

    `<div class="direction-presets-row">` +
      `<button type="button" class="button secondary btn-sm" data-preset-direction="0" ${isDirConfig ? '' : 'disabled'}>0° (Front · LED #1)</button>` +
      `<button type="button" class="button secondary btn-sm" data-preset-direction="90" ${isDirConfig ? '' : 'disabled'}>90° (Right)</button>` +
      `<button type="button" class="button secondary btn-sm" data-preset-direction="180" ${isDirConfig ? '' : 'disabled'}>180° (Back)</button>` +
      `<button type="button" class="button secondary btn-sm" data-preset-direction="270" ${isDirConfig ? '' : 'disabled'}>270° (Left)</button>` +
      `<button type="button" class="button secondary btn-sm" data-nudge-direction="-1" ${isDirConfig ? '' : 'disabled'}>◀ -1 LED</button>` +
      `<button type="button" class="button secondary btn-sm" data-nudge-direction="1" ${isDirConfig ? '' : 'disabled'}>+1 LED ▶</button>` +
    `</div>` +

    `<p class="timestamp" style="margin-top:1.25rem;">` +
      `Column wrap spacing: <strong>n = ${g.n}</strong> (${g.anchors.length} vertical wraps) · ` +
      `Beam width: <strong>${g.w} pixel${g.w > 1 ? 's' : ''}</strong> · ` +
      `<a class="section-link" href="#config-direction">Configure wrap spacing &amp; width ↗</a>` +
    `</p>` +
  `</article>`;
}
function attachLampDialListeners() {
  const svg = document.querySelector('#lamp-dial-svg');
  if (!svg) return;
  let isDragging = false;

  function pointerCoordToOffset(e) {
    const rect = svg.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const clientX = e.touches ? e.touches[0].clientX : e.clientX;
    const clientY = e.touches ? e.touches[0].clientY : e.clientY;
    const dx = clientX - cx;
    const dy = clientY - cy;
    const rad = Math.atan2(dx, -dy);
    const deg = (rad * 180 / Math.PI + 360) % 360;
    const n = directionAlignment || 144;
    return Math.round((deg / 360) * n) % n;
  }

  svg.addEventListener('pointerdown', e => {
    if (stationMode !== 'direction_config') return;
    isDragging = true;
    try { svg.setPointerCapture(e.pointerId); } catch {}
    const s = pointerCoordToOffset(e);
    directionOffset = s;
    updateDialVisuals(s);
    clearTimeout(directionDebounceTimer);
    directionDebounceTimer = setTimeout(() => sendDirection(s), 40);
  });

  svg.addEventListener('pointermove', e => {
    if (!isDragging || stationMode !== 'direction_config') return;
    const s = pointerCoordToOffset(e);
    if (s !== directionOffset) {
      directionOffset = s;
      updateDialVisuals(s);
      clearTimeout(directionDebounceTimer);
      directionDebounceTimer = setTimeout(() => sendDirection(s), 40);
    }
  });

  const stopDrag = e => {
    if (!isDragging) return;
    isDragging = false;
    try { svg.releasePointerCapture(e.pointerId); } catch {}
    clearTimeout(directionDebounceTimer);
    sendDirection(directionOffset);
  };
  svg.addEventListener('pointerup', stopDrag);
  svg.addEventListener('pointercancel', stopDrag);
}
function directionalityPage() {
  const isDirConfig = stationMode === 'direction_config';
  const val = directionAlignment || 144;
  const count = Math.floor((192 - 1) / val) + 1;
  const litText = count === 1 ? '1 LED lit (LED #1)' : `${count} of 192 LEDs lit`;
  const maxBw = maxBeamWidthForAlignment(val);
  const bw = Math.min(directionBeamWidth || 1, maxBw);
  directionBeamWidth = bw;
  const radius = Math.floor((bw - 1) / 2);
  const bwDesc = bw === 1 ? '1 pixel (original LED only)' : `${bw} pixels (±${radius} on each side plus original LED)`;
  return `<article class="card directionality-mode-card">` +
    `<div class="directionality-toggle-row">` +
      `<div class="directionality-toggle-info">` +
        `<h2>Direction Config Mode</h2>` +
        `<p class="timestamp">Toggle Direction Config mode on or off. When active, the station operates in direction configuration mode.</p>` +
      `</div>` +
      `<label class="switch-control" for="direction-config-toggle">` +
        `<input type="checkbox" id="direction-config-toggle" ${isDirConfig ? 'checked' : ''}>` +
        `<span class="switch-slider" aria-hidden="true"></span>` +
        `<span class="switch-label">${isDirConfig ? 'Active' : 'Inactive'}</span>` +
      `</label>` +
    `</div>` +
  `</article>` +
  `<article class="card direction-controls-card ${isDirConfig ? '' : 'controls-disabled'}">` +
    `<div class="direction-disabled-notice" ${isDirConfig ? 'hidden' : ''}><span aria-hidden="true">🔒</span> Direction config mode is inactive. Activate Direction config mode above to enable direction controls.</div>` +
    `<div class="card-header">` +
      `<div>` +
        `<h2>LED Strip Alignment &amp; Beam Width</h2>` +
        `<p class="timestamp">Align LEDs according to spiral wrap spacing <em>n</em> and set beam width. To steer the beam direction around the lamp, open <a class="section-link" href="#direction">Output → Direction ↗</a>.</p>` +
      `</div>` +
      `<span class="badge ${isDirConfig ? 'badge-recv' : 'empty'}">${isDirConfig ? 'Mode: Direction Config' : 'Mode: Normal'}</span>` +
    `</div>` +
    `<form class="direction-alignment-form" data-alignment-form onsubmit="return false;">` +
      `<div class="slider-field">` +
        `<div class="slider-label-row">` +
          `<label for="direction-alignment-slider"><strong>Alignment Spacing (<em>n</em>)</strong></label>` +
          `<output id="direction-alignment-value" for="direction-alignment-slider" class="slider-output">n = ${val} (${litText})</output>` +
        `</div>` +
        `<input id="direction-alignment-slider" name="alignment" type="range" min="1" max="144" step="1" value="${val}" ${isDirConfig ? '' : 'disabled'}>` +
        `<div class="slider-ticks-row">` +
          `<span>All lit (n=1)</span>` +
          `<span>Spacing 48</span>` +
          `<span>Spacing 72</span>` +
          `<span>Spacing 144</span>` +
        `</div>` +
      `</div>` +
      `<div class="alignment-summary-row">` +
        `<div class="alignment-stat"><span class="stat-label">First Lit LED</span><span class="stat-value">LED #1</span></div>` +
        `<div class="alignment-stat"><span class="stat-label">Next Lit LED</span><span class="stat-value" id="alignment-next-led">${val >= 192 ? 'None (beyond LED 192)' : 'LED #' + (1 + val)}</span></div>` +
        `<div class="alignment-stat"><span class="stat-label">Total Lit LEDs</span><span class="stat-value" id="alignment-lit-count">${count} / 192</span></div>` +
      `</div>` +
      `<div class="alignment-presets-row">` +
        `<button type="button" class="button secondary btn-sm" data-preset-alignment="144" ${isDirConfig ? '' : 'disabled'}>Spacing 144</button>` +
        `<button type="button" class="button secondary btn-sm" data-preset-alignment="72" ${isDirConfig ? '' : 'disabled'}>Spacing 72</button>` +
        `<button type="button" class="button secondary btn-sm" data-preset-alignment="48" ${isDirConfig ? '' : 'disabled'}>Spacing 48</button>` +
        `<button type="button" class="button secondary btn-sm" data-preset-alignment="36" ${isDirConfig ? '' : 'disabled'}>Spacing 36</button>` +
        `<button type="button" class="button secondary btn-sm" data-preset-alignment="12" ${isDirConfig ? '' : 'disabled'}>Spacing 12</button>` +
        `<button type="button" class="button secondary btn-sm" data-preset-alignment="1" ${isDirConfig ? '' : 'disabled'}>All lit (n=1)</button>` +
      `</div>` +
      `<div class="direction-section-divider"></div>` +
      `<div class="card-header">` +
        `<div>` +
          `<h2>Beam Width</h2>` +
          `<p class="timestamp">Increase the number of pixels lit around each anchor LED (±neighbours on each side). The maximum width dynamically adjusts based on alignment spacing <em>n</em> (max = ${maxBw} for <em>n</em> = ${val}).</p>` +
        `</div>` +
      `</div>` +
      `<div class="slider-field">` +
        `<div class="slider-label-row">` +
          `<label for="direction-beam-width-slider"><strong>Beam Width (pixels)</strong></label>` +
          `<output id="direction-beam-width-value" for="direction-beam-width-slider" class="slider-output">Width = ${bw} (${bwDesc})</output>` +
        `</div>` +
        `<input id="direction-beam-width-slider" name="beam_width" type="range" min="1" max="${maxBw}" step="2" value="${bw}" ${isDirConfig ? '' : 'disabled'}>` +
        `<div class="slider-ticks-row">` +
          `<span>1 pixel (center only)</span>` +
          `<span id="beam-width-tick-max">Max: ${maxBw} pixels</span>` +
        `</div>` +
      `</div>` +
      `<div class="alignment-summary-row">` +
        `<div class="alignment-stat"><span class="stat-label">Neighbours per Side</span><span class="stat-value" id="beam-width-radius">${radius === 0 ? 'None (0 on each side)' : '±' + radius + ' LEDs on each side'}</span></div>` +
        `<div class="alignment-stat"><span class="stat-label">Beam Width</span><span class="stat-value" id="beam-width-total">${bw} LEDs per anchor</span></div>` +
        `<div class="alignment-stat"><span class="stat-label">Max Allowed Width</span><span class="stat-value" id="beam-width-max">${maxBw} LEDs (max for n=${val})</span></div>` +
      `</div>` +
      `<div class="alignment-presets-row">` +
        `<button type="button" class="button secondary btn-sm" data-preset-beam-width="1" ${isDirConfig ? '' : 'disabled'}>Width = 1 (original only)</button>` +
        `<button type="button" class="button secondary btn-sm" data-preset-beam-width="3" ${isDirConfig && maxBw >= 3 ? '' : 'disabled'}>Width = 3 (±1)</button>` +
        `<button type="button" class="button secondary btn-sm" data-preset-beam-width="5" ${isDirConfig && maxBw >= 5 ? '' : 'disabled'}>Width = 5 (±2)</button>` +
        `<button type="button" class="button secondary btn-sm" data-preset-beam-width="${maxBw}" ${isDirConfig ? '' : 'disabled'}>Max Width (${maxBw})</button>` +
      `</div>` +
    `</form>` +
  `</article>`;
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
    if (['config-direction', 'config-directionality', 'direction', 'lighting'].includes(view())) render();
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
  if (current === 'overview') content.innerHTML = `<div class="grid">${['radar','light','birdcast'].map(source=>sourceCard(source)).join('')}</div><div class="section-heading"><div><h2>Sound from the station</h2><p>A permanent home for your microphone recordings.</p></div><a class="section-link" href="#recordings">Open library ↗</a></div>${microphone()}<div class="section-heading"><h2>Latest recordings</h2></div><div data-recording-library>${library(3)}</div><div class="section-heading"><div><h2>Station & system services</h2><p>Background boot service and remote Cloudflare tunnel status.</p></div></div><div class="grid services-grid" data-station-services>${stationServicesCards()}</div>`;
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
  else if (current === 'direction') {
    content.innerHTML = directionOutputPage();
    attachLampDialListeners();
  }
  else if (current === 'config-direction' || current === 'config-directionality') {
    content.innerHTML = directionalityPage();
  }
  else if (current === 'config-schedule') content.innerHTML = `<article class="card audio-summary"><div class="audio-copy"><h2>Automatic microphone collection</h2><p>Audio recording runs on a shared server-side schedule. When enabled, the station captures 5 seconds of audio every 120 seconds.</p>${resultError('microphone')}</div><div class="audio-tools"><div data-recording-schedule>${scheduleControls()}</div><span class="badge empty" data-recording-count>${recordings.length} saved</span></div></article><article class="card"><h2>Recording schedule settings</h2>${facts([['Interval','120 seconds (2 minutes)'],['Sample duration','5 seconds'],['Audio format','WAV 48 kHz mono 16-bit PCM'],['State',collection.enabled ? 'Active (running)' : 'Stopped (manual captures only)'],['Next capture',collection.next_recording_at ? time(collection.next_recording_at) : 'None scheduled']])}</article>`;
  else if (current === 'config-settings') content.innerHTML = settingsPage();
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
    if (data.services) systemServices = data.services;
    if (data.directionality?.alignment) directionAlignment = data.directionality.alignment;
    else if (data.lighting?.alignment) directionAlignment = data.lighting.alignment;
    if (data.directionality?.beam_width) directionBeamWidth = data.directionality.beam_width;
    else if (data.lighting?.beam_width) directionBeamWidth = data.lighting.beam_width;
    if (data.directionality?.offset != null) directionOffset = data.directionality.offset;
    else if (data.lighting?.direction_offset != null) directionOffset = data.lighting.direction_offset;
    const alignmentSlider = document.querySelector('#direction-alignment-slider');
    const widthSlider = document.querySelector('#direction-beam-width-slider');
    if (alignmentSlider && document.activeElement !== alignmentSlider && document.activeElement !== widthSlider) {
      alignmentSlider.value = directionAlignment;
      if (widthSlider) widthSlider.value = directionBeamWidth;
      updateAlignmentDisplay(directionAlignment);
      updateBeamWidthDisplay(directionBeamWidth, directionAlignment);
    }
    syncLightingControls();
    syncStationModeUI();
    Object.keys(historyKeys).forEach(source=>historyKeys[source]='');
    radarLoadedAt = requestedAt;
    scheduleRadarExpiry();
    configurationError=data.configuration?.error || '';
    if (configurationError) notice(configurationError,true);
    regions = data.regions.sort((a,b)=>a.name.localeCompare(b.name));
    if (!regions.some(item=>item.code===region)) region = regions.some(item=>item.code==='US-CA') ? 'US-CA' : regions[0]?.code || 'US-CA';
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
    notice((data.saved===false ? 'Lighting mode updated. ' : 'Lighting preferences saved. ')+(lighting.status==='controller_unavailable' ? 'Controller delivery failed; preferences remain saved.' : ''),lighting.status==='controller_unavailable');
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
  if (event.target.id === 'direction-alignment-slider') {
    const n = parseInt(event.target.value, 10);
    directionAlignment = n;
    const maxBw = maxBeamWidthForAlignment(n);
    const bwSlider = document.querySelector('#direction-beam-width-slider');
    if (bwSlider) {
      bwSlider.max = maxBw;
      if (directionBeamWidth > maxBw) {
        directionBeamWidth = maxBw;
        bwSlider.value = maxBw;
      }
    }
    updateAlignmentDisplay(n);
    updateBeamWidthDisplay(directionBeamWidth, n);
    clearTimeout(alignmentDebounceTimer);
    alignmentDebounceTimer = setTimeout(() => {
      sendAlignment(n, directionBeamWidth);
    }, 60);
    return;
  }
  if (event.target.id === 'direction-beam-width-slider') {
    const bw = parseInt(event.target.value, 10);
    directionBeamWidth = bw;
    updateBeamWidthDisplay(bw, directionAlignment);
    clearTimeout(alignmentDebounceTimer);
    alignmentDebounceTimer = setTimeout(() => {
      sendAlignment(directionAlignment, bw);
    }, 60);
    return;
  }
  const form = event.target.closest('[data-lighting-settings]');
  if (!form) return;
  form.dataset.dirty = 'true';
  if (event.target.name === 'color-picker') form.querySelector('[name="color"]').value = event.target.value;
  if (event.target.name === 'color' && /^#[0-9a-f]{6}$/i.test(event.target.value)) form.querySelector('[name="color-picker"]').value = event.target.value;
  if (event.target.name === 'brightness') form.querySelector('output').textContent = `${Math.round(Number(event.target.value) / 255 * 100)}%`;
});
content.addEventListener('submit', event=>{
  const form = event.target.closest('[data-lighting-settings]');
  if (!form) return;
  event.preventDefault();
  setLightingMode(null, {...(lighting.mode === 'auto' ? {} : {brightness:Number(form.querySelector('[name="brightness"]').value)}),color:form.querySelector('[name="color"]').value});
});
content.addEventListener('click',event=>{
  const presetBtn = event.target.closest('[data-preset-alignment]');
  if (presetBtn) {
    const n = parseInt(presetBtn.dataset.presetAlignment, 10);
    directionAlignment = n;
    const slider = document.querySelector('#direction-alignment-slider');
    if (slider) slider.value = n;
    const maxBw = maxBeamWidthForAlignment(n);
    const bwSlider = document.querySelector('#direction-beam-width-slider');
    if (bwSlider) {
      bwSlider.max = maxBw;
      if (directionBeamWidth > maxBw) {
        directionBeamWidth = maxBw;
        bwSlider.value = maxBw;
      }
    }
    updateAlignmentDisplay(n);
    updateBeamWidthDisplay(directionBeamWidth, n);
    clearTimeout(alignmentDebounceTimer);
    sendAlignment(n, directionBeamWidth);
    return;
  }
  const presetBwBtn = event.target.closest('[data-preset-beam-width]');
  if (presetBwBtn) {
    const bw = parseInt(presetBwBtn.dataset.presetBeamWidth, 10);
    directionBeamWidth = bw;
    const bwSlider = document.querySelector('#direction-beam-width-slider');
    if (bwSlider) bwSlider.value = bw;
    updateBeamWidthDisplay(bw, directionAlignment);
    clearTimeout(alignmentDebounceTimer);
    sendAlignment(directionAlignment, bw);
    return;
  }
  const presetDirBtn = event.target.closest('[data-preset-direction]');
  if (presetDirBtn) {
    const deg = parseFloat(presetDirBtn.dataset.presetDirection);
    const n = directionAlignment || 144;
    const s = Math.round((deg / 360) * n) % n;
    directionOffset = s;
    updateDialVisuals(s);
    clearTimeout(directionDebounceTimer);
    sendDirection(s);
    return;
  }
  const nudgeDirBtn = event.target.closest('[data-nudge-direction]');
  if (nudgeDirBtn) {
    const step = parseInt(nudgeDirBtn.dataset.nudgeDirection, 10);
    const n = directionAlignment || 144;
    const s = ((directionOffset + step) % n + n) % n;
    directionOffset = s;
    updateDialVisuals(s);
    clearTimeout(directionDebounceTimer);
    sendDirection(s);
    return;
  }
  const maximum = event.target.closest('[data-lighting-max]');
  if (maximum) {
    const form = content.querySelector('[data-lighting-settings]');
    const color = form?.querySelector('[name="color"]')?.value || lighting.color || '#ffd6aa';
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
  if (event.target.id === 'direction-alignment-slider') {
    const n = parseInt(event.target.value, 10);
    directionAlignment = n;
    const maxBw = maxBeamWidthForAlignment(n);
    const bwSlider = document.querySelector('#direction-beam-width-slider');
    if (bwSlider) {
      bwSlider.max = maxBw;
      if (directionBeamWidth > maxBw) {
        directionBeamWidth = maxBw;
        bwSlider.value = maxBw;
      }
    }
    updateAlignmentDisplay(n);
    updateBeamWidthDisplay(directionBeamWidth, n);
    clearTimeout(alignmentDebounceTimer);
    sendAlignment(n, directionBeamWidth);
    return;
  }
  if (event.target.id === 'direction-beam-width-slider') {
    const bw = parseInt(event.target.value, 10);
    directionBeamWidth = bw;
    updateBeamWidthDisplay(bw, directionAlignment);
    clearTimeout(alignmentDebounceTimer);
    sendAlignment(directionAlignment, bw);
    return;
  }
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
  if (event.target.id === 'region' || source || rangeSource) saveBrowserPreferences();
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
const browserPreferencesKey = 'bioglow-display-v1';
let backupPreview = null;
function browserPreferences() {
  return {region,metrics:{...historyMetrics},ranges:{...historyRanges},animal:selectedAnimal};
}
function applyBrowserPreferences(value) {
  if (!value || typeof value !== 'object') return;
  region = typeof value.region === 'string' && /^US-[A-Z]{2}$/.test(value.region) ? value.region : 'US-CA';
  Object.keys(historyMetrics).forEach(source=>{
    if (graphMetrics[source].some(([key])=>key===value.metrics?.[source])) historyMetrics[source]=value.metrics[source];
  });
  Object.keys(historyRanges).forEach(source=>{
    if (rangeOptions.some(([key])=>key===value.ranges?.[source])) historyRanges[source]=value.ranges[source];
  });
  selectedAnimal = typeof value.animal==='string' ? value.animal : '';
  Object.keys(historyKeys).forEach(source=>historyKeys[source]='');
}
function saveBrowserPreferences() {
  try {
    localStorage.setItem(browserPreferencesKey,JSON.stringify(browserPreferences()));
    browserPersistenceError='';
    return true;
  } catch {
    browserPersistenceError='Browser storage is unavailable. Display choices will not survive reload in this browser.';
    notice(browserPersistenceError,true);
    return false;
  }
}
function settingsPage() {
  return `<article class="card"><h2>Automatic saving</h2><p>Lighting preferences are saved automatically on this server. Startup always uses Normal / Auto, with microphone scheduling stopped. Calibration stays dormant until Direction Config is entered.</p><p>Display choices are saved only in this browser. Backups include shared lighting preferences and this browser's display choices, without recordings, readings, results, secrets, or runtime modes.</p><p role="status">${escapeHtml(configurationError)} ${escapeHtml(browserPersistenceError)}</p><button class="button" id="download-settings">Download JSON backup</button><p><label>Upload JSON backup <input id="upload-settings" type="file" accept=".json,application/json"></label></p><div id="settings-preview"></div></article>`;
}
function previewTable(current, proposed) {
  return `<table><thead><tr><th>Setting</th><th>Current</th><th>Proposed</th></tr></thead><tbody>${Object.keys(proposed).map(key=>`<tr><th>${escapeHtml(key)}</th><td>${escapeHtml(JSON.stringify(current[key]))}</td><td>${escapeHtml(JSON.stringify(proposed[key]))}</td></tr>`).join('')}</tbody></table>`;
}
document.addEventListener('click',async event=>{
  try {
    if (event.target.id==='download-settings') {
      const snapshot=await api('/v1/configuration');
      const backup={format:'bioglow-configuration',version:1,exported_at:new Date().toISOString(),shared:snapshot.shared,browser:browserPreferences()};
      const url=URL.createObjectURL(new Blob([JSON.stringify(backup,null,2)+'\n'],{type:'application/json'}));
      const link=document.createElement('a');
      link.href=url; link.download='bioglow-configuration-'+backup.exported_at.replace(/[:.]/g,'-')+'.json';
      link.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
    }
    if (event.target.id==='cancel-settings') {
      backupPreview=null; document.querySelector('#settings-preview').replaceChildren();
    }
    if (event.target.id==='confirm-settings' && backupPreview) {
      event.target.disabled=true;
      const result=await api('/v1/configuration/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({backup:backupPreview.proposed,revision:backupPreview.current.revision})});
      backupPreview=null;
      applyBrowserPreferences(result.browser);
      const browserSaved=saveBrowserPreferences();
      let refreshWarning='';
      try { await loadCache(); } catch(error) { refreshWarning=' Status refresh failed: '+error.message; }
      if(view()==='config-settings') render();
      notice('Shared settings durably restored. '+(browserSaved ? 'Display preferences saved in this browser.' : browserPersistenceError)+(result.lighting.status==='controller_unavailable' ? ' Controller delivery failed; settings remain saved.' : '')+refreshWarning,!browserSaved || result.lighting.status==='controller_unavailable');
    }
  } catch(error) {
    backupPreview=null;
    const panel=document.querySelector('#settings-preview');
    if(panel) panel.textContent='Restore did not complete. Upload again for a fresh preview. '+error.message;
    notice(error.message,true);
  }
});
document.addEventListener('change',async event=>{
  if(event.target.id!=='upload-settings') return;
  backupPreview=null;
  const panel=document.querySelector('#settings-preview');
  panel.textContent='';
  try {
    const file=event.target.files[0];
    if(!file) return;
    if(file.size>4096) throw new Error('Configuration file exceeds 4 KB.');
    const backup=JSON.parse(await file.text());
    const preview=await api('/v1/configuration/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({backup})});
    backupPreview=preview;
    panel.innerHTML='<h3>Shared lighting preferences</h3>'+previewTable(preview.current.shared,preview.proposed.shared)+'<h3>This browser only</h3>'+previewTable(browserPreferences(),preview.proposed.browser)+'<p>Confirm to replace the complete settings snapshot. Station mode and recording state remain as they are.</p><button class="button" id="confirm-settings">Confirm restore</button> <button class="button secondary" id="cancel-settings">Cancel</button>';
  } catch(error) { panel.textContent='Invalid backup: '+error.message; }
});
try {
  const stored=localStorage.getItem(browserPreferencesKey);
  if(stored) applyBrowserPreferences(JSON.parse(stored));
  saveBrowserPreferences();
} catch {
  browserPersistenceError='Browser preferences could not be loaded. Defaults are in use; display persistence may be unavailable.';
  notice(browserPersistenceError,true);
}

render();
Promise.allSettled([loadCache(),loadLibrary()]).then(results=>{render();const failure = results.find(item=>item.status === 'rejected');if(failure)notice(failure.reason.message,true);});
