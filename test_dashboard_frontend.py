"""Check browser-side state without hardware, sockets, or a browser installation.

The minimal DOM double checks behavior, not layout or actual media playback.
"""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node is unavailable for the JavaScript state check")
class FrontendTests(unittest.TestCase):
    def test_cached_load_navigation_actions_and_local_radar_expiry(self):
        script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
let clock = 0, delayLatest = 0, replacement = '', requests = [], timers = [], queried = [];
const cancelledTimers = new Set();
let analysisState = {state:'queued'};
let radarUnavailable = false;
const windowEvents = {}, contentEvents = {}, nodes = new Map();
function element() {
  const classes = new Set();
  return {innerHTML:'',textContent:'',hidden:false,dataset:{},inert:false,
    classList:{toggle(name,on){if(on)classes.add(name);else classes.delete(name);return on;},contains(name){return classes.has(name);},remove(name){classes.delete(name);}},
    addEventListener(name,fn){this.listeners ??= {};this.listeners[name]=fn;},
    setAttribute(){},removeAttribute(){},focus(){},
    querySelector(selector){queried.push(selector);return {html:this.innerHTML,querySelector:()=>null,replaceWith(node){replacement=node.html;}};},
    get firstElementChild(){return {html:this.innerHTML};}};
}
const document = {
  querySelector(selector){if(!nodes.has(selector))nodes.set(selector,element());return nodes.get(selector);},
  querySelectorAll(){return ['overview','radar','light','lighting','birdcast','recordings'].map(name=>Object.assign(element(),{dataset:{view:name}}));},
  createElement:element,addEventListener(){},
};
const data = {status:'ok',sources:{radar:{http_status:200,result:{fresh:true,presence:'moving',
  moving_distance_cm:120,moving_energy_score:70,serial_connected:true,age_seconds:.1,
  stale_after_seconds:2,timestamp:'2026-10-11T12:00:00Z'}}},regions:[{code:'US-CA',name:'California'},{code:'US-NY',name:'New York'}]};
let recordings=[];
const context=vm.createContext({document,console,URL,Object,Date,location:{hash:''},performance:{now:()=>clock},
  window:{matchMedia:()=>({matches:false,addEventListener(){}}),addEventListener:(name,fn)=>{windowEvents[name]=fn;}},
  setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},clearTimeout:id=>cancelledTimers.add(id),
  fetch:async(path,options)=>{
    requests.push({path,options});
    let body;
    if(path==='/v1/latest'){
      clock+=delayLatest;body=JSON.parse(JSON.stringify(data));
      if(radarUnavailable)body.sources.radar={http_status:503,result:{fresh:false,status:'unavailable',error:{message:'Radar disconnected'}}};
    }
    else if(path==='/presence') {
      body=radarUnavailable ? {fresh:false,status:'unavailable',error:{message:'Radar disconnected'}} : data.sources.radar.result;
      return {ok:!radarUnavailable,json:async()=>JSON.parse(JSON.stringify(body))};
    }
    else if(path.startsWith('/v1/history?'))body={entries:[],animals:[],total:0,sampled:false,start:0,end:1};
    else if(path.startsWith('/v1/animal-names?'))body={names:{'Buteo jamaicensis':'Red-tailed Hawk'}};
    else if(path==='/v1/recordings' && !options.method)body={recordings};
    else if(path.endsWith('/analysis')) {
      body={status:'ok',analysis:options.method==='POST' ? {state:'queued'} : analysisState};
    }
    else if(path==='/v1/snapshots'){
      const region=JSON.parse(options.body).region;
      body={status:'ok',region:{code:region},metrics:{birds_in_flight:123}};
      data.sources.birdcast={http_status:200,result:body};
    }else if(path==='/v1/recordings' && options.method==='POST'){
      body={recording_id:'a'.repeat(32),created_at:'2026-10-11T12:00:00Z',byte_length:960044,
        download_path:'/v1/recordings/'+'a'.repeat(32),analysis:{state:'queued'}};recordings=[body];
    }else throw new Error('Unexpected request '+path);
    return {ok:true,json:async()=>JSON.parse(JSON.stringify(body))};
  },
});
async function main(){
  vm.runInContext(fs.readFileSync('web/dashboard.js','utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(requests.map(r=>r.path),['/v1/latest','/v1/recordings']);
  assert.match(document.querySelector('#content').innerHTML,/Fresh frame/);
  assert.ok(!document.querySelector('#content').innerHTML.includes('data-history-metric'), 'Overview contains no graphs or graph controls');
  const expiry=timers.find(timer=>timer.delay>=1900 && timer.delay<=2000);
  assert.ok(expiry);
  clock=3000;expiry.fn();
  assert.match(replacement,/Unavailable/);
  assert.ok(!replacement.includes('>moving</div>'));
  assert.match(vm.runInContext("sourceCard('radar',true)",context),/This radar frame is stale/);
  await timers.findLast(timer=>timer.delay===200).fn();
  assert.match(replacement,/Fresh frame/);
  assert.match(replacement,/>moving<\/div>/);
  radarUnavailable=true;
  await timers.findLast(timer=>timer.delay===200).fn();
  assert.match(replacement,/Radar disconnected/);
  assert.ok(!replacement.includes('>moving</div>'));
  radarUnavailable=false;
  await timers.findLast(timer=>timer.delay===200).fn();
  assert.match(replacement,/Fresh frame/);
  const pendingRadarPoll=timers.findLast(timer=>timer.delay===200);
  const beforeNavigation=requests.length;
  context.location.hash='#birdcast';windowEvents.hashchange();
  await pendingRadarPoll.fn();
  assert.equal(document.querySelector('#page-title').textContent,'Migration outlook');
  assert.equal(requests.filter(r=>r.path==='/v1/latest').length,4,'Navigation cancels radar polling on other pages');
  await new Promise(resolve=>setImmediate(resolve));
  vm.runInContext(`histories={
    light:[{read_at:'2026-10-11T12:00:00Z',http_status:200,result:{lux:0,ambient_counts:0,white_counts:2}},
      {read_at:'2026-10-11T12:00:01Z',http_status:503,result:{}},
      {read_at:'2026-10-11T12:00:02Z',http_status:200,result:{lux:42,ambient_counts:30,white_counts:60}}],
    radar:[{read_at:'2026-10-11T12:00:00Z',http_status:200,result:{presence:'none'}},
      {read_at:'2026-10-11T12:00:01Z',http_status:200,result:{presence:'both'}}],
    birdcast:[{read_at:'2026-10-11T12:00:00Z',http_status:200,result:{region:{code:'US-CA'},metrics:{birds_in_flight:123}}},
      {read_at:'2026-10-11T12:00:01Z',http_status:200,result:{region:{code:'US-NY'},metrics:{birds_in_flight:999}}}]
  }`,context);
  const lightGraph=vm.runInContext("historyGraph('light')",context);
  assert.match(lightGraph,/2 plotted readings/);
  assert.match(lightGraph,/Luminance \(lux\) over read time/);
  vm.runInContext("sources.light={http_status:200,result:{lux:0,ambient_counts:0,white_counts:42,gain:0.125,integration_time_ms:100,resolution_lux_per_count:0.5376}}",context);
  const lightReading=vm.runInContext("sourceCard('light',true)",context);
  assert.match(lightReading,/Ambient light \(raw\)/);
  assert.match(lightReading,/0 counts/);
  assert.match(lightReading,/42 counts/);
  assert.match(lightReading,/0.125×/);
  assert.match(lightReading,/100 ms/);
  assert.match(lightReading,/0.5376 lux\/count/);
  vm.runInContext("historyMetrics.light='ambient_counts'",context);
  assert.match(vm.runInContext("historyPlot('light')",context),/Ambient light \(raw counts\)/);
  vm.runInContext("historyMetrics.light='white_counts'",context);
  assert.match(vm.runInContext("historyPlot('light')",context),/White channel \(raw counts\)/);
  vm.runInContext("historyMetrics.light='lux'",context);

  assert.equal((lightGraph.match(/<circle /g)||[]).length,2,'Zero lux is a valid reading');
  assert.ok(!lightGraph.includes('<path d="M '),'Failed reads break plotted lines');
  const presenceGraph=vm.runInContext("historyGraph('radar')",context);
  assert.match(presenceGraph,/H .* V /,'Presence uses steps between categorical states');
  assert.match(presenceGraph,/none/);
  assert.match(presenceGraph,/both/);
  const migrationGraph=vm.runInContext("historyGraph('birdcast')",context);
  assert.match(migrationGraph,/123/);
  assert.ok(!migrationGraph.includes('999'),'Migration history filters by region');
  assert.match(vm.runInContext("historyGraph('light')",context),/Measured/);
  assert.match(migrationGraph,/Half hour/);
  assert.match(migrationGraph,/All time/);
  const unchangedPage=document.querySelector('#content').innerHTML;
  const metricSelect={dataset:{historyMetric:'radar'},value:'moving_distance_cm'};
  document.activeElement=metricSelect;
  document.querySelector('#content').listeners.change({target:metricSelect});
  assert.equal(vm.runInContext('historyMetrics.radar',context),'moving_distance_cm');
  assert.equal(document.querySelector('#content').innerHTML,unchangedPage,'Selecting a metric does not replace the controls');
  assert.equal(document.activeElement,metricSelect);
  assert.match(vm.runInContext("historyPlot('radar')",context),/moving distance/);
  document.querySelector('#content').listeners.change({target:{dataset:{historyRange:'light'},value:'30m'}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(requests.some(r=>r.path==='/v1/history?source=light&range=30m'));
  assert.ok(queried.includes('[data-reading="radar"]'),'Radar updates target only readings, preserving graph controls');
  await vm.runInContext("loadAnimalNames(['Buteo jamaicensis'])",context);
  assert.match(vm.runInContext("animalLabel('Buteo jamaicensis')",context),/Red-tailed Hawk.*Buteo jamaicensis/);
  vm.runInContext(`histories.microphone=[{read_at:'2026-10-11T12:00:00Z',http_status:200,result:{timestamp:'2026-10-11T11:00:00Z',animals:[{scientific_name:'Buteo jamaicensis',score:-2}]}}];historyInfo.microphone={animals:['Buteo jamaicensis']};selectedAnimal='Buteo jamaicensis'`,context);
  // Use the entry times when no server axis metadata is present.
  vm.runInContext("delete historyInfo.microphone;historyInfo.microphone=undefined",context);
  const animalGraph=vm.runInContext("historyGraph('microphone')",context);
  assert.match(animalGraph,/Red-tailed Hawk/);
  assert.match(animalGraph,/no fixed minimum or maximum/);
  assert.match(animalGraph,/over recording time/);
  assert.match(animalGraph,/-2/);
  document.querySelector('#content').listeners.change({target:{id:'region',value:'US-NY'}});
  await vm.runInContext("run('birdcast')",context);
  const snapshotRequest=requests.find(r=>r.path==='/v1/snapshots');
  assert.equal(JSON.parse(snapshotRequest.options.body).region,'US-NY');
  assert.match(document.querySelector('#content').innerHTML,/123/);
  context.location.hash='#recordings';windowEvents.hashchange();
  await vm.runInContext("run('microphone')",context);
  assert.match(document.querySelector('#content').innerHTML,/<audio controls preload="none"/);
  assert.match(document.querySelector('#content').innerHTML,/download="a{32}\.wav"/);
  assert.equal(requests.filter(r=>r.path==='/v1/recordings' && r.options.method==='POST').length,1);
  assert.equal(vm.runInContext("escapeHtml('<script>')",context),'&lt;script&gt;');
  // Analysis updates touch only their panel, leaving audio and focused controls in place.
  const panel = {innerHTML:'',querySelector:()=>null};
  const page = document.querySelector('#content');
  page.querySelector=selector=>selector.startsWith('[data-analysis=') ? panel : null;
  const audio = {currentTime:4.5,paused:false};
  document.activeElement = audio;
  const beforePoll = page.innerHTML;
  analysisState={state:'completed',candidates:[{scientific_name:'<img onerror=bad>',score:8}],
    windows:[{start_seconds:0,end_seconds:5,candidates:[]}]};
  const poll = timers.findLast(timer=>timer.delay===3000);
  assert.ok(poll,'A queued recording schedules a status poll');
  await poll.fn();
  assert.equal(page.innerHTML,beforePoll,'Polling must not replace the recording/audio subtree');
  assert.equal(document.activeElement,audio);
  assert.equal(audio.currentTime,4.5);
  assert.match(panel.innerHTML,/&lt;img onerror=bad&gt;/);
  assert.match(panel.innerHTML,/Scores are not confidence percentages or animal counts/);
  assert.match(panel.innerHTML,/Five-second window scores/);
  assert.ok(!panel.innerHTML.includes('<img onerror'));
  assert.equal(vm.runInContext("recordings[0].analysis.state",context),'completed');
  vm.runInContext("recordings[0].analysis={state:'failed',error:'Try again'}",context);
  await vm.runInContext("identifyAnimals('a'.repeat(32))",context);
  assert.equal(requests.filter(r=>r.path.endsWith('/analysis') && r.options.method==='POST').length,1);
  assert.equal(vm.runInContext("recordings[0].analysis.state",context),'queued');
  const pendingPoll = timers.findLast(timer=>timer.delay===3000);
  context.location.hash='#birdcast';windowEvents.hashchange();
  const countBeforeNavigation = requests.length;
  await pendingPoll.fn();
  assert.equal(requests.length,countBeforeNavigation,'Navigation cancels polling for hidden recordings');
  assert.ok(cancelledTimers.has(timers.indexOf(pendingPoll)+1));
  context.location.hash='#lighting';windowEvents.hashchange();
  assert.equal(document.querySelector('#page-title').textContent,'WLED strip lighting');
  assert.match(page.innerHTML,/data-lighting-settings/);
  assert.ok(!page.innerHTML.includes('data-history-metric'),'Controller page contains no sensor graph');
  assert.ok(!page.innerHTML.includes('data-action="light"'),'Controller page contains no lux read action');
  assert.ok(!vm.runInContext("sourceCard('light',true)",context).includes('data-lighting-settings'),'Lux page contains no fixture controls');
  const wledPoll=timers.findLast(timer=>timer.delay===1000);
  assert.ok(wledPoll,'Controller page polls cached acknowledgements once per second');
  const beforeWledPoll=page.innerHTML;
  await wledPoll.fn();
  assert.equal(page.innerHTML,beforeWledPoll,'Status updates preserve controller controls');
  vm.runInContext("lighting={device:{led_count:192,covered_leds:192,firmware:'0.14.4'}}",context);
  assert.match(vm.runInContext('lightingDevice()',context),/192 \/ 192 pixels/);
  const brightnessInput={value:'80'}, colorInput={value:'#112233'}, output={textContent:''};
  const form={dataset:{dirty:'true'},contains:()=>false,querySelector:selector=>selector==='[name="brightness"]' ? brightnessInput : selector==='[name="color"]' ? colorInput : output};
  const summary={textContent:''};
  page.querySelector=selector=>selector==='[data-lighting-settings]' ? form : selector==='[data-lighting-summary]' ? summary : null;
  page.querySelectorAll=()=>[];
  vm.runInContext("lighting={connection:'usb',mode:'on',status:'ok',manual_brightness:120,color:'#abcdef'}",context);
  vm.runInContext('syncLightingControls()',context);
  assert.equal(brightnessInput.value,'80','Polling preserves unapplied edits');
  form.dataset.dirty='false';
  form.contains=()=>true;
  vm.runInContext('syncLightingControls()',context);
  assert.equal(brightnessInput.value,'80','Polling preserves focused controls');
  form.contains=()=>false;
  vm.runInContext('syncLightingControls()',context);
  assert.equal(brightnessInput.value,120);
  assert.equal(colorInput.value,'#abcdef');
  assert.match(vm.runInContext('lightingControls()',context),/type="color"/);
  assert.match(vm.runInContext('lightingControls()',context),/type="range"/);
  assert.match(vm.runInContext('lightingControls()',context),/SP803E · WLED-ESP32 · USB/);
  const originalFetch=context.fetch;
  context.fetch=async(path,options)=>{
    if(path==='/v1/lighting') {
      requests.push({path,options});
      const requested=JSON.parse(options.body);
      data.lighting={connection:'usb',mode:requested.mode || 'on',status:'pending',manual_brightness:requested.brightness ?? 80,color:requested.color || '#112233'};
      return {ok:true,json:async()=>({lighting:data.lighting})};
    }
    return originalFetch(path,options);
  };
  brightnessInput.value='80';colorInput.value='#112233';
  let prevented=false;
  page.listeners.submit({preventDefault(){prevented=true;},target:{closest:()=>form}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(prevented);
  assert.deepEqual(JSON.parse(requests.findLast(r=>r.path==='/v1/lighting').options.body),{brightness:80,color:'#112233'});
  assert.equal(vm.runInContext('lighting.mode',context),'on');
  assert.equal(form.dataset.dirty,'false');
  for (const mode of ['off','on','auto','distance_test']) {
    await vm.runInContext(`setLightingMode('${mode}')`,context);
    assert.deepEqual(JSON.parse(requests.findLast(r=>r.path==='/v1/lighting').options.body),{mode});
    assert.equal(vm.runInContext('lighting.mode',context),mode);
  }

  page.listeners.click({target:{closest:selector=>selector==='[data-lighting-max]' ? {} : null}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(JSON.parse(requests.findLast(r=>r.path==='/v1/lighting').options.body),{brightness:255,color:'#112233',mode:'on'});
  assert.equal(vm.runInContext('lighting.manual_brightness',context),255);
  assert.match(vm.runInContext('lightingControls()',context),/Full brightness · 100%/);
  vm.runInContext("lighting.mode='distance_test';lighting.distance_cm=350;lighting.led_index=96;lighting.test_state='tracking'",context);
  assert.match(vm.runInContext('lightingControls()',context),/data-lighting-appearance disabled/);
  assert.match(vm.runInContext('lightingSummary()',context),/350 cm · LED 97 \/ 192/);
  vm.runInContext("lighting.led_index=null;lighting.test_state='sensor_unavailable';lighting.sensor_error='Radar unavailable'",context);
  assert.match(vm.runInContext('lightingControls()',context),/Radar unavailable/);
  assert.match(vm.runInContext('lightingSummary()',context),/sensor unavailable/);
  vm.runInContext("lighting.mode='on';delete lighting.sensor_error",context);

  vm.runInContext("lighting.device={confirmed_color:'#112233',confirmed_brightness:255,confirmed_on:true,segment_brightness:255,current_limit_ma:3000}",context);
  assert.match(vm.runInContext('lightingDevice()',context),/100% \(255 \/ 255\)/);
  assert.match(vm.runInContext('lightingDevice()',context),/#112233/);
  assert.match(vm.runInContext('lightingDevice()',context),/3,000 mA/);
  assert.equal(context.location.href,'/distance-test','Activating dev mode opens its page');
  const oldHash=context.location.hash;
  context.location.hash='';context.location.pathname='/distance-test';
  const writesBefore=requests.filter(r=>r.options?.method==='POST').length;
  vm.runInContext('render()',context);
  assert.equal(vm.runInContext('view()',context),'distance-test');
  assert.match(page.innerHTML,/Single-LED test/);
  assert.match(page.innerHTML,/LED 1: 1 m or closer/);
  assert.match(page.innerHTML,/LED 192: 6 m or farther/);
  assert.match(page.innerHTML,/data-reading="radar"/);
  assert.match(page.innerHTML,/Start distance test/);
  assert.doesNotMatch(page.innerHTML,/data-history="radar"/);
  assert.equal(requests.filter(r=>r.options?.method==='POST').length,writesBefore,'Opening the page does not activate lights');
  const devPoll=timers[vm.runInContext('radarPollTimer',context)-1];
  assert.equal(devPoll.delay,500,'Dev radar and lighting poll every half second');
  data.lighting={mode:'distance_test',status:'ok',distance_cm:120,led_index:64};
  const requestsBefore=requests.length;
  await devPoll.fn();
  assert.equal(requests.length,requestsBefore+1,'Radar and lighting share one passive request');
  assert.equal(requests.at(-1).path,'/v1/latest');
  assert.equal(vm.runInContext('lighting.led_index',context),64);
  context.location.pathname='/';context.location.hash=oldHash;
  delayLatest=2500;
  await vm.runInContext('loadCache()',context);
  assert.equal(vm.runInContext("currentEntry('radar').http_status",context),503,'Network delay must count toward radar freshness');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which("node"), "-e", script],
                                cwd=Path(__file__).resolve().parent,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
