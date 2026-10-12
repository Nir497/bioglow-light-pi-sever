'use strict';
const names = {overview:'Overview',radar:'Radar',light:'Light',birdcast:'BirdCast',recordings:'Recordings'};
const titles = {overview:'Station overview',radar:'Presence & movement',light:'Ambient light',birdcast:'Migration outlook',recordings:'Sound library'};
const descriptions = {overview:'Your sensors, migration data, and sound — in one place.',radar:'The newest radar frame, with distances, energy scores, and connection status.',light:'Measure the light around your station with the VEML7700.',birdcast:'Fetch the latest migration snapshot for a US state.',recordings:'Capture a moment. Listen again. Manage your saved recordings.'};
let sources = {}, recordings = [], regions = [], region = 'US-CA', loadingLibrary = true, libraryError = '', cacheError = '';
let histories = {};
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
  radar:[['presence','Presence state'],['moving_distance_cm','Moving distance (cm)'],['stationary_distance_cm','Stationary distance (cm)'],['moving_energy_score','Moving energy score'],['stationary_energy_score','Stationary energy score']],
  light:[['lux','Luminance (lux)']],
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
const view = () => Object.hasOwn(names, location.hash.slice(1)) ? location.hash.slice(1) : 'overview';
function notice(message, error = false) { const box = document.querySelector('#notice'); box.textContent = message; box.classList.toggle('error',error); box.hidden = !message; }
async function api(path, options = {}) {
  const response = await fetch(path, {...options, cache:'no-store'});
  let data;
  try { data = await response.json(); } catch { throw new Error('The station returned an unreadable response.'); }
  if (!response.ok) {
    const error = new Error(data.error?.message || data.analysis?.error || `Request failed (${response.status}).`);
    error.data = data;
    throw error;
  }
  return data;
}
function details(data, label = 'View complete data') { return `<details class="details"><summary>${escapeHtml(label)}</summary><pre>${escapeHtml(JSON.stringify(data,null,2))}</pre></details>`; }
function action(source, label) { return `<button class="button" data-action="${source}" ${pending.has(source) ? 'disabled aria-busy="true"' : ''}>${pending.has(source) ? (source === 'microphone' ? 'Recording 5 seconds…' : 'Requesting…') : label}</button>`; }
function currentEntry(source) {
  const entry = sources[source];
  if (source !== 'radar' || entry?.http_status !== 200) return entry;
  const data = entry.result;
  const age = (data.age_seconds ?? 0) + (performance.now() - radarLoadedAt) / 1000;
  if (age <= data.stale_after_seconds) return entry;
  const {presence,moving_distance_cm,stationary_distance_cm,moving_energy_score,stationary_energy_score,...status} = data;
  return {http_status:503,result:{...status,fresh:false,age_seconds:age,status:'unavailable',
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
  if (!['overview','radar'].includes(view())) return;
  radarPollTimer = setTimeout(async()=>{
    if (generation !== radarPollGeneration) return;
    const started = performance.now();
    let entry;
    try {
      const latest = await api('/v1/latest');
      entry = latest.sources.radar;
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
  },1000);
}
function updateRadarReading() {
  const reading = content.querySelector('[data-reading="radar"]');
  if (!reading) return;
  const wrapper = document.createElement('div');
  wrapper.innerHTML = sourceCard('radar',view()==='radar');
  const replacement = wrapper.querySelector('[data-reading="radar"]');
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
  let body = `<div class="big-value">${value}</div><div class="value-caption">${caption}</div><div class="timestamp">${success ? `Measured ${time(stamp)}` : 'No current result'}${entry?.requested_at ? `<br>Last request ${time(entry.requested_at)}` : ''}</div>${resultError(source)}`;
  if (entry?.result.history_error) body += `<p class="error-message">${escapeHtml(entry.result.history_error)}</p>`;
  if (expanded && source === 'radar') body += facts([
    ['Moving distance',data.moving_distance_cm == null ? '—' : `${number(data.moving_distance_cm)} cm`],
    ['Stationary distance',data.stationary_distance_cm == null ? '—' : `${number(data.stationary_distance_cm)} cm`],
    ['Moving energy score',number(data.moving_energy_score)],['Stationary energy score',number(data.stationary_energy_score)],
    ['Serial connected',entry ? (entry.result.serial_connected ? 'Yes' : 'No') : '—'],
    ['Frame age',entry?.result.age_seconds == null ? '—' : `${number(entry.result.age_seconds)} s`]]) + '<p class="timestamp">Energy is a sensor score from 0–100, not a percentage or probability. Frames older than two seconds are unavailable.</p>';
  if (expanded && source === 'birdcast') {
    const metrics = data.metrics || {};
    body += facts([['Birds crossed',number(metrics.total_birds_crossed)],['Direction',escapeHtml(metrics.direction || '—')],['Speed',metrics.speed_mph == null ? '—' : `${number(metrics.speed_mph)} mph`],['Altitude',metrics.altitude_ft == null ? '—' : `${number(metrics.altitude_ft)} ft`],['Region',escapeHtml(data.region?.name || data.region?.code || '—')],['Selected night',escapeHtml(data.selected_night || '—')]]);
    if (data.warnings?.length) body += `<ul class="warning-list">${data.warnings.map(item=>`<li>${escapeHtml(item)}</li>`).join('')}</ul>`;
    if (data.source?.url) {
      try { const url = new URL(data.source.url); if (url.protocol === 'https:' && url.hostname === 'dashboard.birdcast.org') body += `<p class="timestamp"><a href="${escapeHtml(url.href)}" target="_blank" rel="noopener noreferrer">Open BirdCast source ↗</a></p>`; } catch {}
    }
  }
  if (expanded && source === 'light') body += '<p class="timestamp">The server collects light every ten seconds. If the sensor is disconnected, reconnect it and choose Read now.</p>';
  if (expanded && entry?.http_status !== 200 && entry?.last_success && source !== 'radar') body += `<div class="history"><p>Previous successful result · historical data</p>${details(entry.last_success,'View previous successful result')}</div>`;
  const control = source === 'birdcast' ? action('birdcast','Fetch BirdCast') : action(source,'Read now');
  return `<article class="card" data-source="${source}"><div data-reading="${source}"><div class="card-header"><div class="source-label"><div class="source-icon" aria-hidden="true">${meta[0]}</div><div><h2>${meta[1]}</h2><small>${meta[2]}</small></div></div>${badge(source)}</div>${body}<div class="card-actions">${control}${expanded ? '' : `<a class="section-link" href="#${source}">Explore data ↗</a>`}</div>${expanded ? details(entry || {status:'not_requested'}) : ''}</div>${expanded ? historyGraph(source) : ''}</article>`;
}
function historyGraph(source) {
  const options = source === 'microphone' ? (historyInfo.microphone?.animals || []).map(animal=>[animal,animalLabel(animal)]) : graphMetrics[source];
  const metric = source === 'microphone' ? selectedAnimal : historyMetrics[source];
  const metricControl = `<label>${source === 'microphone' ? 'Animal' : 'Graph'} <select data-history-metric="${source}" aria-label="${source === 'microphone' ? 'Animal' : names[source]} graph metric">${options.map(([key,title])=>`<option value="${escapeHtml(key)}" ${key===metric?'selected':''}>${escapeHtml(title)}</option>`).join('')}</select></label>`;
  const rangeControl = `<label>Time range <select data-history-range="${source}" aria-label="${source} time range">${rangeOptions.map(([key,title])=>`<option value="${key}" ${key===historyRanges[source]?'selected':''}>${title}</option>`).join('')}</select></label>`;
  return `<section class="reading-history" data-history="${source}"><div class="history-controls">${metricControl}${rangeControl}<button class="button secondary" data-history-refresh="${source}">Refresh graph</button></div>${source==='microphone' ? scoreExplanation : ''}<div data-chart="${source}">${historyPlot(source)}</div></section>`;
}
function historyPlot(source) {
  const metric = source === 'microphone' ? selectedAnimal : historyMetrics[source];
  const entries = (histories[source] || []).filter(entry=>source !== 'birdcast' || entry.result.region?.code === region);
  const states = {none:0,moving:1,stationary:2,both:3};
  const points = entries.map(entry=>{
    const value = source === 'radar' && metric === 'presence' ? states[entry.result.presence] :
      source === 'birdcast' ? entry.result.metrics?.[metric] : source === 'microphone' ? entry.result.animals?.find(animal=>animal.scientific_name===metric)?.score : entry.result[metric];
    return {entry,x:Date.parse(source === 'microphone' ? entry.result.timestamp : entry.read_at),y:entry.http_status === 200 && typeof value === 'number' && Number.isFinite(value) ? value : null};
  }).filter(point=>Number.isFinite(point.x));
  const valid = points.filter(point=>point.y !== null);
  const label = source === 'microphone' ? `${metric ? animalLabel(metric) : 'Animal'} model score` : graphMetrics[source].find(item=>item[0]===metric)[1];
  if (historyErrors[source]) return `<p class="error-message">${escapeHtml(historyErrors[source])}</p>`;
  if (!valid.length) return `<p>No saved ${escapeHtml(label.toLowerCase())} readings in this time range.</p>`;
  const info = historyInfo[source];
  const first = info ? info.start*1000 : points[0].x, last = info ? info.end*1000 : points[points.length-1].x;
  const categorical = source === 'radar' && metric === 'presence';
  const low = categorical ? 0 : Math.min(0,...valid.map(point=>point.y));
  const high = categorical ? 3 : Math.max(low+1,...valid.map(point=>point.y));
  const x = point=>50+(last===first ? 250 : (point.x-first)/(last-first)*500);
  const y = point=>170-(point.y-low)/(high-low)*140;
  let previous = null;
  const marks = points.map(point=>{
    if (point.y === null) { previous = null; return ''; }
    const line = previous ? `<path d="M ${x(previous)} ${y(previous)} ${categorical ? `H ${x(point)} V ${y(point)}` : `L ${x(point)} ${y(point)}`}"/>` : '';
    previous = point;
    const value = categorical ? point.entry.result.presence : point.y;
    return `${line}<circle cx="${x(point)}" cy="${y(point)}" r="3" tabindex="0"><title>${escapeHtml(value)} · Read ${time(point.entry.read_at)} · Measured ${time(point.entry.result.timestamp || point.entry.result.retrieved_at)}</title></circle>`;
  }).join('');
  const ticks = categorical ? ['none','moving','stationary','both'].map((state,index)=>`<text x="45" y="${170-index/3*140}" text-anchor="end">${state}</text>`).join('') : `<text x="45" y="32" text-anchor="end">${number(high)}</text><text x="45" y="172" text-anchor="end">${number(low)}</text>`;
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
  collectionTimer = setTimeout(async()=>{
    try {
      const latest = await api('/v1/latest');
      if (generation!==collectionGeneration) return;
      collection = latest.collection || collection;
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
  },10000);
}
function render() {
  const current = view();
  document.querySelector('#breadcrumb').textContent = names[current];
  document.querySelector('#page-title').textContent = titles[current];
  document.querySelector('#page-description').textContent = descriptions[current];
  document.querySelectorAll('[data-view]').forEach(link => { if(link.dataset.view === current) link.setAttribute('aria-current','page'); else link.removeAttribute('aria-current'); });
  if (current === 'overview') content.innerHTML = `<div class="grid">${['radar','light','birdcast'].map(source=>sourceCard(source)).join('')}</div><div class="section-heading"><div><h2>Sound from the station</h2><p>A permanent home for your microphone recordings.</p></div><a class="section-link" href="#recordings">Open library ↗</a></div>${microphone()}<div class="section-heading"><h2>Latest recordings</h2></div><div data-recording-library>${library(3)}</div>`;
  else if (current === 'recordings') content.innerHTML = `${microphone()}<article class="card"><h2>Animal identification over time</h2>${historyGraph('microphone')}</article><div class="section-heading"><div><h2>All recordings</h2><p>Newest first · delete individual recordings using their Delete recording button</p></div><button class="button secondary" data-action="library" ${pending.has('library') ? 'disabled' : ''}>↻ Refresh library</button></div><div data-recording-library>${library()}</div>`;
  else {
    const selector = current === 'birdcast' ? `<div class="selector"><label for="region">US region</label><select id="region">${regions.map(item=>`<option value="${escapeHtml(item.code)}" ${item.code === region ? 'selected' : ''}>${escapeHtml(item.name)}</option>`).join('')}</select></div>` : '';
    content.innerHTML = `${selector}${sourceCard(current,true)}`;
  }
  scheduleAnalysisPoll();
  scheduleRadarPoll();
  scheduleHistoryPoll();
  scheduleCollectionPoll();
}
async function loadCache() {
  const requestedAt = performance.now();
  try { const data = await api('/v1/latest'); sources = data.sources; collection = data.collection || collection; Object.keys(historyKeys).forEach(source=>historyKeys[source]=''); radarLoadedAt = requestedAt; scheduleRadarExpiry(); regions = data.regions.sort((a,b)=>a.name.localeCompare(b.name)); cacheError = ''; }
  catch (error) { cacheError = error.message; throw error; }
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
content.addEventListener('click',event=>{
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
  const button = event.target.closest('[data-action]'); if (button) run(button.dataset.action);
});
content.addEventListener('change',event=>{
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
