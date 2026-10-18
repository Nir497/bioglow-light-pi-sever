"""Exercise graph controls in Chromium with synthetic data and no hardware."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("chromium"), "Chromium is unavailable")
class BrowserTests(unittest.TestCase):
    def test_graph_controls_survive_live_refresh_and_plot_animals(self):
        root = Path(__file__).resolve().parent
        setup = r'''
const now = new Date().toISOString();
const reading = {fresh:true,presence:'moving',moving_distance_cm:120,moving_energy_score:70,
  serial_connected:true,age_seconds:0,stale_after_seconds:2,timestamp:now};
const browserRequests=[];
const recordingId='a'.repeat(32);
let browserRecordings=[{recording_id:recordingId,created_at:now,byte_length:960044,
  download_path:'/v1/recordings/'+recordingId,analysis:{state:'completed',candidates:[{scientific_name:'Buteo jamaicensis',score:7.5}]}}];
let browserCollection={enabled:false,recording:false,duration_seconds:5,interval_seconds:120};
let browserLighting={mode:'off',status:'ok',on:false};
window.fetch=async(path,options={})=>{
  browserRequests.push(path);
  let body;
  if(path==='/v1/latest')body={sources:{radar:{http_status:200,result:reading}},lighting:browserLighting,collection:browserCollection,regions:[{code:'US-CA',name:'California'}]};
  else if(path==='/v1/lighting'){
    browserLighting={mode:JSON.parse(options.body).mode,status:'ok',on:true,distance_cm:350,
      led_index:96,confirmed_led_index:96,test_state:'tracking'};
    Object.assign(reading,{range_estimate_cm:350,moving_distance_cm:650,stationary_distance_cm:350,
      timestamp:new Date().toISOString()});
    body={lighting:browserLighting};
  }
  else if(path==='/v1/recording-schedule'){browserCollection.enabled=JSON.parse(options.body).enabled;body={collection:browserCollection};}
  else if(path==='/v1/recordings')body={recordings:browserRecordings};
  else if(path==='/v1/recordings/'+recordingId && options.method==='DELETE'){browserRecordings=[];body={deleted:true};}
  else if(path.startsWith('/v1/animal-names'))body={names:{'Buteo jamaicensis':'Red-tailed Hawk'}};
  else if(path.startsWith('/v1/history')){
    const source=new URL(path,'http://test').searchParams.get('source');
    const result=source==='microphone' ? {timestamp:now,animals:[{scientific_name:'Buteo jamaicensis',score:7.5}]} :
      source==='light' ? {lux:42,timestamp:now} : source==='birdcast' ? {region:{code:'US-CA'},metrics:{birds_in_flight:123}} : reading;
    body={entries:[{read_at:now,http_status:200,result}],total:1,sampled:false,animals:source==='microphone'?['Buteo jamaicensis']:[],start:Date.parse(now)/1000-3600,end:Date.parse(now)/1000+1};
  }else throw new Error('Unexpected '+path);
  return {ok:true,json:async()=>body};
};
'''
        checks = r'''
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const check=(ok,message)=>{if(!ok)throw new Error(message);};
setTimeout(async()=>{
  try {
    check(!document.querySelector('[data-history-metric]'),'Overview must not show graphs');
    location.hash='#radar';await pause(100);
    check(document.querySelector('.radar-locator .locator-target')?.getAttribute('data-estimated-range')==='120','Top radar object position graphic missing target at 120 cm');
    check(document.querySelector('.radar-banner').textContent.includes('PRESENT_ACTIVE'),'Derived presence state missing on Radar page');
    check(document.querySelectorAll('.radar-gate-row').length===9,'Radar page must render all 9 radial distance gates (G0-G8)');
    const metric=document.querySelector('[data-history-metric="radar"]');
    metric.focus();metric.value='presence_state';metric.dispatchEvent(new Event('change',{bubbles:true}));
    check(document.querySelector('[data-chart="radar"] svg').getAttribute('aria-label').includes('Derived presence state'),'Derived presence state graph failed');
    metric.value='moving_distance_cm';metric.dispatchEvent(new Event('change',{bubbles:true}));
    check(document.querySelector('[data-chart="radar"] svg').getAttribute('aria-label').includes('Moving distance'),'Metric selection must change plot');
    await pause(1100);
    check(document.querySelector('[data-history-metric="radar"]')===metric,'Live refresh replaced graph selector');
    check(document.activeElement===metric,'Live refresh lost selector focus');
    check(metric.value==='moving_distance_cm','Live refresh reset selected metric');
    const range=document.querySelector('[data-history-range="radar"]');
    range.value='1d';range.dispatchEvent(new Event('change',{bubbles:true}));await pause(50);
    check(browserRequests.includes('/v1/history?source=radar&range=1d'),'Day range did not load history');
    location.hash='#light';await pause(100);
    check(document.querySelector('[data-chart="light"] svg'),'Light graph missing');
    location.hash='#birdcast';await pause(100);
    const migration=document.querySelector('[data-history-metric="birdcast"]');
    migration.value='speed_mph';migration.dispatchEvent(new Event('change',{bubbles:true}));
    check(document.querySelector('[data-chart="birdcast"]').textContent.includes('speed'),'Migration selector did not change metric');
    location.hash='#recordings';await pause(100);
    check(document.querySelector('[data-history-metric="microphone"]').textContent.includes('Red-tailed Hawk'),'Animal common name missing');
    check(document.querySelector('[data-chart="microphone"] svg').getAttribute('aria-label').includes('recording time'),'Animal scores must use recording time');
    check(document.querySelector('[data-history="microphone"]').textContent.includes('no fixed minimum or maximum'),'Score scale explanation missing');
    check(document.querySelector('[data-action="microphone"]').textContent.includes('5 seconds'),'Manual capture duration is not five seconds');
    document.querySelector('[data-schedule-recording="start"]').click();await pause(50);
    check(browserCollection.enabled,'Start did not enable periodic recording');
    check(document.querySelector('[data-schedule-recording="stop"]'),'Stop control missing');
    document.querySelector('[data-schedule-recording="stop"]').click();await pause(50);
    check(!browserCollection.enabled,'Stop did not disable periodic recording');
    const playingAudio=document.querySelector('[data-recording] audio');
    const animalChoice=document.querySelector('[data-history-metric="microphone"]');
    animalChoice.focus();await pause(10100);
    check(document.querySelector('[data-recording] audio')===playingAudio,'Periodic library refresh replaced audio');
    check(document.querySelector('[data-history-metric="microphone"]')===animalChoice,'Periodic refresh replaced animal selector');
    check(document.activeElement===animalChoice,'Periodic refresh lost selector focus');
    const deletion=document.querySelector('[data-delete-recording]');
    window.confirm=()=>false;deletion.click();await pause(50);
    check(browserRecordings.length===1,'Cancelled deletion removed a recording');
    window.confirm=()=>true;deletion.click();await pause(100);
    check(browserRecordings.length===0,'Confirmed deletion did not call DELETE');
    check(!document.querySelector('[data-recording]'),'Deleted recording remains visible');
    check(document.querySelector('#content').textContent.includes('Your first recording starts here'),'Empty recording library missing');
    location.hash='#distance-test';await pause(100);
    const startTest=document.querySelector('[data-lighting-mode="distance_test"]');
    check(startTest,'Dev test start button missing');
    startTest.click();await pause(150);
    check(document.querySelector('[data-lighting-summary]').textContent.includes('350 cm · Target LED 97'),'LED target range missing');
    check(document.querySelector('[data-lighting-summary]').textContent.includes('Confirmed: LED 97'),'Confirmed output missing');
    check(document.querySelector('[data-estimated-range]').dataset.estimatedRange==='350','Radar and LED use different ranges');
    Object.assign(reading,{range_estimate_cm:null,timestamp:new Date().toISOString()});
    Object.assign(browserLighting,{distance_cm:null,led_index:null,confirmed_led_index:null,on:false,
      test_state:'sensor_unavailable',sensor_error:'No usable filtered range'});
    await pause(650);
    check(!document.querySelector('[data-estimated-range]'),'Filtered null fell back to raw 650 cm');
    check(document.querySelector('[data-lighting-error]').textContent.includes('No usable filtered range'),'Sensor error not shown');
    check(document.querySelector('[data-lighting-mode="distance_test"]')===startTest,'Polling replaced the dev controls');
    document.body.dataset.browserCheck='passed';
  }catch(error){document.body.dataset.browserCheck='failed: '+error.message;}
},50);
'''
        html = (root/"web/index.html").read_text().replace('<script src="/dashboard.js" defer></script>', '')
        html = html.replace('<link rel="stylesheet" href="/dashboard.css">', '')
        scripts = setup + "\n" + (root/"web/dashboard.js").read_text() + "\n" + checks
        html = html.replace('</body>', '<script>'+scripts+'</script></body>')
        with tempfile.TemporaryDirectory() as directory:
            page = Path(directory)/"graphs.html"
            page.write_text(html, encoding="utf-8")
            result = subprocess.run([shutil.which("chromium"), "--headless", "--no-sandbox",
                                     "--disable-gpu", "--disable-dev-shm-usage", "--no-proxy-server",
                                     "--user-data-dir="+directory+"/profile", "--virtual-time-budget=15000",
                                     "--dump-dom", page.as_uri()], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        self.assertIn('data-browser-check="passed"', result.stdout,
                      result.stdout[-1800:]+result.stderr[-500:])
