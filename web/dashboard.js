'use strict';
const names = {overview:'Overview',radar:'Radar',light:'Light',birdcast:'BirdCast',recordings:'Recordings'};
const titles = {overview:'Station overview',radar:'Presence & movement',light:'Ambient light',birdcast:'Migration outlook',recordings:'Sound library'};
const descriptions = {overview:'Your sensors, migration data, and sound — in one place.',radar:'The newest radar frame, with distances, energy scores, and connection status.',light:'Measure the light around your station with the VEML7700.',birdcast:'Fetch the latest migration snapshot for a US state.',recordings:'Capture a moment. Listen again. Every recording stays here.'};
let sources = {}, recordings = [], regions = [], region = 'US-CA', loadingLibrary = true, libraryError = '', cacheError = '';
let radarLoadedAt = performance.now(), radarExpiryTimer;
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
  if (!response.ok) throw new Error(data.error?.message || `Request failed (${response.status}).`);
  return data;
}
function details(data, label = 'View complete data') { return `<details class="details"><summary>${escapeHtml(label)}</summary><pre>${escapeHtml(JSON.stringify(data,null,2))}</pre></details>`; }
function action(source, label) { return `<button class="button" data-action="${source}" ${pending.has(source) ? 'disabled aria-busy="true"' : ''}>${pending.has(source) ? (source === 'microphone' ? 'Recording 10 seconds…' : 'Requesting…') : label}</button>`; }
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
    const card = content.querySelector('[data-source="radar"]');
    if (!card) return;
    const wrapper = document.createElement('div');
    wrapper.innerHTML = sourceCard('radar',view()==='radar');
    card.replaceWith(wrapper.firstElementChild);
  },Math.max(0,remaining)*1000+25);
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
  if (expanded && source === 'light') body += '<p class="timestamp">This is the last requested measurement, not a live stream. If the sensor is disconnected, reconnect it and choose Read now.</p>';
  if (expanded && entry?.http_status !== 200 && entry?.last_success && source !== 'radar') body += `<div class="history"><p>Previous successful result · historical data</p>${details(entry.last_success,'View previous successful result')}</div>`;
  const control = source === 'birdcast' ? action('birdcast','Fetch BirdCast') : action(source,'Read now');
  return `<article class="card" data-source="${source}"><div class="card-header"><div class="source-label"><div class="source-icon" aria-hidden="true">${meta[0]}</div><div><h2>${meta[1]}</h2><small>${meta[2]}</small></div></div>${badge(source)}</div>${body}<div class="card-actions">${control}${expanded ? '' : `<a class="section-link" href="#${source}">Explore data ↗</a>`}</div>${expanded ? details(entry || {status:'not_requested'}) : ''}</article>`;
}
function facts(items) { return `<dl class="facts">${items.map(([label,value])=>`<div class="fact"><dt>${label}</dt><dd>${value}</dd></div>`).join('')}</dl>`; }
function recordingCard(item) {
  return `<article class="card record-row"><div><h3>${time(item.created_at)}</h3><p>10 seconds · Mono · 48 kHz · 16-bit PCM</p><p>${number(item.byte_length / 1024)} KB</p></div><audio controls preload="none" aria-label="Recording from ${time(item.created_at)}" src="${escapeHtml(item.download_path)}"></audio><a class="button secondary" href="${escapeHtml(item.download_path)}" download="${escapeHtml(item.recording_id)}.wav">↓ Download</a>${details(item,'Recording details & integrity')}</article>`;
}
function library(limit = null) {
  if (loadingLibrary) return '<div class="card empty-state"><p>Loading recordings…</p></div>';
  if (libraryError) return `<div class="card empty-state"><h3>Library unavailable</h3><p>${escapeHtml(libraryError)}</p></div>`;
  if (!recordings.length) return '<div class="card empty-state"><div class="source-icon" aria-hidden="true">≋</div><h3>Your first recording starts here</h3><p>Choose Record 10 seconds to capture audio from the station microphone. Completed recordings are kept for playback and download.</p></div>';
  return `<div class="record-list">${(limit ? recordings.slice(0,limit) : recordings).map(recordingCard).join('')}</div>`;
}
function microphone() {
  return `<article class="card audio-summary"><div class="audio-copy"><div class="sound-icon" aria-hidden="true">${[16,24,38,26,52,65,37,50,26,40,20,12].map(height=>`<i style="--height:${height}px"></i>`).join('')}</div><h2>Listen to your surroundings</h2><p>Record ten seconds from the station microphone. Every capture stays in your library, even after another app downloads it.</p>${resultError('microphone')}</div><div class="audio-tools">${action('microphone','● Record 10 seconds')}<span class="badge empty">${recordings.length} saved</span></div></article>`;
}
function render() {
  const current = view();
  document.querySelector('#breadcrumb').textContent = names[current];
  document.querySelector('#page-title').textContent = titles[current];
  document.querySelector('#page-description').textContent = descriptions[current];
  document.querySelectorAll('[data-view]').forEach(link => { if(link.dataset.view === current) link.setAttribute('aria-current','page'); else link.removeAttribute('aria-current'); });
  if (current === 'overview') content.innerHTML = `<div class="grid">${['radar','light','birdcast'].map(source=>sourceCard(source)).join('')}</div><div class="section-heading"><div><h2>Sound from the station</h2><p>A permanent home for your microphone recordings.</p></div><a class="section-link" href="#recordings">Open library ↗</a></div>${microphone()}<div class="section-heading"><h2>Latest recordings</h2></div>${library(3)}`;
  else if (current === 'recordings') content.innerHTML = `${microphone()}<div class="section-heading"><div><h2>All recordings</h2><p>Newest first · recordings are never automatically deleted</p></div><button class="button secondary" data-action="library" ${pending.has('library') ? 'disabled' : ''}>↻ Refresh library</button></div>${library()}`;
  else {
    const selector = current === 'birdcast' ? `<div class="selector"><label for="region">US region</label><select id="region">${regions.map(item=>`<option value="${escapeHtml(item.code)}" ${item.code === region ? 'selected' : ''}>${escapeHtml(item.name)}</option>`).join('')}</select></div>` : '';
    content.innerHTML = `${selector}${sourceCard(current,true)}`;
  }
}
async function loadCache() {
  const requestedAt = performance.now();
  try { const data = await api('/v1/latest'); sources = data.sources; radarLoadedAt = requestedAt; scheduleRadarExpiry(); regions = data.regions.sort((a,b)=>a.name.localeCompare(b.name)); cacheError = ''; }
  catch (error) { cacheError = error.message; throw error; }
}
async function loadLibrary() {
  try { const data = await api('/v1/recordings'); recordings = data.recordings; libraryError = ''; }
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
content.addEventListener('click',event=>{ const button = event.target.closest('[data-action]'); if (button) run(button.dataset.action); });
content.addEventListener('change',event=>{ if(event.target.id === 'region') region = event.target.value; });
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
