/* No browser/GPU dependencies: polling lifecycle and benchmark admission. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const timers = new Map(); let nextTimer = 0, calls = 0, finish;
const nodes = new Map();
const node = key => {
  if (!nodes.has(key)) nodes.set(key, {querySelectorAll: () => [], disabled: false, innerHTML: '', textContent: ''});
  return nodes.get(key);
};
const ctx = vm.createContext({hw: {}, $: node, Set, Date, String, console,
  setInterval: fn => { timers.set(++nextTimer, fn); return nextTimer; },
  clearInterval: id => timers.delete(id),
  api: () => { calls++; return new Promise(resolve => { finish = resolve; }); },
  document: {querySelectorAll: () => []}, t: k => k, esc: String, GB: String, LANG: 'en'});
vm.runInContext(fs.readFileSync('static/js/llm.js', 'utf8'), ctx);
(async () => {
  vm.runInContext('startLlm(); startLlm(); pollLlm();', ctx);
  assert.equal(timers.size, 1); assert.equal(calls, 1);
  vm.runInContext('stopLlm()', ctx); assert.equal(timers.size, 0);
  finish({ok: false, error: 'unavailable'}); await new Promise(setImmediate);
  assert.equal(node('#llmCfg').innerHTML, ''); // Late response on another tab ignored.
  vm.runInContext('startLlm()', ctx); assert.equal(calls, 2);
  finish({ok: false, error: 'unavailable'}); await new Promise(setImmediate);
  assert.match(node('#llmLoaded').innerHTML, /llm.runtime_unknown/);
  vm.runInContext('stopLlm(); renderInventory = () => {}; renderBench = (j,ol,cfg,inst,running,disabled) => { hw.result = {inst,disabled}; }', ctx);
  ctx.payload={servers:[{kind:'Ollama',state:'online',loaded:[],installed:[{name:'embed',capabilities:['embedding']},{name:'text',capabilities:['completion']}]}],guard:{blocked:true,reason:'strata_resident'},config:{}};
  vm.runInContext('renderLlm(payload)',ctx);
  assert.equal(ctx.hw.result.inst.length, 1); assert.equal(ctx.hw.result.inst[0].name,'text'); assert.equal(ctx.hw.result.disabled,true);
  ctx.payload.switch_control={available:true};
  ctx.payload.switch={status:'running',phase:'unloading_ollama',target:'strata'};
  vm.runInContext('renderLlm(payload)',ctx);
  assert.match(node('#llmSwitch').innerHTML, /llm.switch_phase_unloading_ollama/);
  assert.match(node('#llmSwitch').innerHTML, /id="llmSwitchStrata"[^>]*disabled/);
  assert.equal(ctx.hw.llmSwitchBusy,true);
  assert.equal(ctx.hw.result.disabled,true);
  ctx.payload.switch={status:'error',error:'not_unloaded',target:'strata'};
  vm.runInContext('renderLlm(payload)',ctx);
  assert.match(node('#llmSwitch').innerHTML, /llm.switch_error_not_unloaded/);
  assert.equal(ctx.hw.llmSwitchBusy,false);
  ctx.payload.servers.push({kind:'Strata', loaded:[{name:'strata'}]});
  ctx.payload.switch={status:'error',error:'interrupted',recoverable:true};
  vm.runInContext('renderLlm(payload)',ctx);
  assert.match(node('#llmSwitch').innerHTML, /llm.recover_strata/);
  assert.doesNotMatch(node('#llmSwitch').innerHTML, /id="llmSwitchStrata"[^>]*disabled/);
  console.log('PASS: one timer, no overlapping requests, exit cleanup, late-response isolation, unknown status, text-only benchmarks, guard disable and switch progress/errors');
})().catch(e => { console.error(e); process.exitCode=1; });
