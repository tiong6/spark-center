/* Run the actual monitor lifecycle with delayed and failed HTTP responses. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const timers = new Map(), pending = [], nodes = new Map();
let timerId = 0, now = 100000;
const node = key => {
  if (!nodes.has(key)) {
    const classes = new Set(key === '#tab-hardware' ? ['hide'] : []);
    nodes.set(key, {innerHTML:'', textContent:'', querySelectorAll:()=>[], addEventListener:()=>{},
      classList:{contains:k=>classes.has(k), add:k=>classes.add(k), remove:k=>classes.delete(k),
        toggle:(k,v)=>v?classes.add(k):classes.delete(k)}});
  }
  return nodes.get(key);
};
class Clock extends Date { static now() { return now; } }
const ctx = vm.createContext({$:node, console, Date:Clock, window:{},
  document:{querySelectorAll:()=>[]}, localStorage:{getItem:()=>null,setItem:()=>{}},
  t:(k,v)=>k+(v?' '+JSON.stringify(v):''), esc:String, LANG:'en',
  setInterval:fn=>{timers.set(++timerId,fn);return timerId;}, clearInterval:id=>timers.delete(id),
  api:path=>new Promise((resolve,reject)=>pending.push({path,resolve,reject}))});
for (const file of ['hw-common.js','monitor.js','hardware.js']) vm.runInContext(fs.readFileSync('static/js/'+file,'utf8'),ctx);
vm.runInContext('hw.static = {}; renderMon = () => {}; renderClockPanel = () => {}; renderGpuAlert = () => {}; meter = () => "";',ctx);
const run = code => vm.runInContext(code,ctx);
const flush = () => new Promise(setImmediate);
const good = {ok:true,memory:{total:1000,available:500,swap_total:0,swap_free:0},sensors:[]};
(async()=>{
  await run('startMon();');
  assert.equal(timers.size,2);
  assert.equal(pending.length,2);
  for(let i=0;i<5;i++) for(const tick of timers.values()) tick();
  assert.equal(pending.length,2,'slow live and Wi-Fi requests must not overlap');
  now+=11000;
  for(const tick of timers.values()) tick();
  assert.match(node('#monFreshness').textContent,/mon.no_live_data/);
  pending[0].resolve(good);pending[1].resolve({ok:true});await flush();
  assert.match(node('#monFreshness').textContent,/mon.live_fresh/);
  now+=11000;
  for(const tick of timers.values()) tick();
  assert.match(node('#monFreshness').textContent,/mon.live_stale/);
  pending[2].reject(new Error('offline'));pending[3].reject(new Error('offline'));await flush();
  assert.match(node('#monFreshness').textContent,/mon.live_stale/);
  run('pollHw()');pending[4].resolve(good);await flush();
  assert.match(node('#monFreshness').textContent,/mon.live_fresh/);
  run('pollHw()');const last=run('hw.lastLiveAt');run('stopHw()');
  assert.equal(timers.size,0);
  now+=5000;pending[5].resolve(good);await flush();
  assert.equal(run('hw.lastLiveAt'),last,'late response after tab exit ignored');
  await run('startMon()');run('stopHw()');node('#tab-hardware').classList.remove('hide');
  run('pollHw(true)');const count=pending.length;
  pending[6].resolve(good);pending[7].resolve({ok:true});await flush();
  assert.equal(pending.length,count+1,'hardware entry gets a fresh reading after old request finishes');
  pending[8].resolve(good);await flush();
  run('pollHw(true)');pending[9].resolve({ok:false});await flush();
  assert.equal(run('hw.liveFailed'),true);
  console.log('PASS: no overlapping live/Wi-Fi polls; stalled/failed/stale/recovered states; tab exit and hardware entry');
})().catch(e=>{console.error(e);process.exitCode=1;});
