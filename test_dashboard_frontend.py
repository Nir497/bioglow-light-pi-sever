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
let clock = 0, delayLatest = 0, replacement = '', requests = [], timers = [];
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
    querySelector(){return {replaceWith(node){replacement=node.html;}};},
    get firstElementChild(){return {html:this.innerHTML};}};
}
const document = {
  querySelector(selector){if(!nodes.has(selector))nodes.set(selector,element());return nodes.get(selector);},
  querySelectorAll(){return ['overview','radar','light','birdcast','recordings'].map(name=>Object.assign(element(),{dataset:{view:name}}));},
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
    if(path==='/v1/latest'){clock+=delayLatest;body=data;}
    else if(path==='/presence') {
      body=radarUnavailable ? {fresh:false,status:'unavailable',error:{message:'Radar disconnected'}} : data.sources.radar.result;
      return {ok:!radarUnavailable,json:async()=>JSON.parse(JSON.stringify(body))};
    }
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
  const expiry=timers.find(timer=>timer.delay>=1900 && timer.delay<=2000);
  assert.ok(expiry);
  clock=3000;expiry.fn();
  assert.match(replacement,/Unavailable/);
  assert.ok(!replacement.includes('>moving</div>'));
  assert.match(vm.runInContext("sourceCard('radar',true)",context),/This radar frame is stale/);
  await timers.findLast(timer=>timer.delay===1000).fn();
  assert.match(replacement,/Fresh frame/);
  assert.match(replacement,/>moving<\/div>/);
  radarUnavailable=true;
  await timers.findLast(timer=>timer.delay===1000).fn();
  assert.match(replacement,/Radar disconnected/);
  assert.ok(!replacement.includes('>moving</div>'));
  radarUnavailable=false;
  await timers.findLast(timer=>timer.delay===1000).fn();
  assert.match(replacement,/Fresh frame/);
  const pendingRadarPoll=timers.findLast(timer=>timer.delay===1000);
  const beforeNavigation=requests.length;
  context.location.hash='#birdcast';windowEvents.hashchange();
  await pendingRadarPoll.fn();
  assert.equal(document.querySelector('#page-title').textContent,'Migration outlook');
  assert.equal(requests.length,beforeNavigation,'Navigation cancels radar polling on other pages');
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
  const countBeforeNavigation = requests.length;
  context.location.hash='#birdcast';windowEvents.hashchange();
  await pendingPoll.fn();
  assert.equal(requests.length,countBeforeNavigation,'Navigation cancels polling for hidden recordings');
  assert.ok(cancelledTimers.has(timers.indexOf(pendingPoll)+1));
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
