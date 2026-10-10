/* Runtime status and persisted file inventory are intentionally separate. */
hw.llmBenchModel = null; hw.llmN = 128; hw.llmC = 1;
hw.llmPending = new Set();
const llmBytes = n => n == null ? '—' : (n / 1073741824).toFixed(1) + ' GiB';
const selStyle = 'background:#0f1110;color:var(--text);border:1px solid #3a3f3c;border-radius:6px;padding:6px 10px;font:inherit;font-size:13px';
async function llmApi(path, body) {
  try { return await api(path, body); }
  catch (e) { return {ok: false, error: String(e)}; }
}
function startLlm() {
  hw.llmActive = true;
  if (hw.llmTimer) return;
  pollLlm();
  hw.llmTimer = setInterval(pollLlm, 10000);
}
function stopLlm() {
  hw.llmActive = false;
  if (hw.llmTimer) clearInterval(hw.llmTimer);
  hw.llmTimer = null;
}
async function pollLlm() {
  if (hw.llmPolling) return;
  hw.llmPolling = true;
  try {
    const j = await llmApi('/api/llm');
    if (!hw.llmActive) return;
    if (!j.ok) {
      $('#llmCfg').innerHTML = `<div class="empty">${t("llm.query_failed", {error: esc(j.error)})}</div>`;
      $('#llmLoaded').innerHTML = `<div class="sub1">${t("llm.runtime_unknown")}</div>`;
      document.querySelectorAll('#tab-llm button, #tab-llm select').forEach(b => b.disabled = true);
      return;
    }
    renderLlm(j);
  } finally { hw.llmPolling = false; }
}
function renderLlm(j) {
  const ol = j.servers.find(s => s.kind === 'Ollama');
  const cfg = j.config || {}, guard = j.guard || {blocked: true, reason: 'guard_unavailable'};
  const known = ol?.state === 'online';
  $('#llmSrvSub').textContent = j.servers.map(s => `${s.kind} · ${t(s.state === 'unavailable' ? 'llm.unavailable' : 'llm.online')}`).join(' / ');
  $('#llmCfg').innerHTML = `<div class="panel notice ${guard.blocked ? 'warn' : ''}" style="padding:10px 14px">${t('llm.' + (guard.blocked ? guard.reason : 'guard_ready'))}<div class="sub1">${t('llm.guard_scope')}</div></div>`;
  if (guard.last_block) $('#llmCfg').innerHTML += `<div class="sub1">${t('llm.last_block', {time: esc(new Date(guard.last_block.at * 1000).toLocaleString(LANG)), model: esc(guard.last_block.model)})}</div>`;
  $('#llmCfg').innerHTML += `<details><summary>${t('llm.service_details')}</summary>${j.servers.map(s => `<div class="sub1">${esc(s.kind)} ${esc(s.version || '—')} · ${esc(s.url)}</div>`).join('')}<div class="sub1">OLLAMA_NUM_PARALLEL: ${cfg.num_parallel ?? '—'} · OLLAMA_MAX_LOADED_MODELS: ${cfg.max_loaded ?? '—'}</div></details>`;
  const loaded = j.servers.flatMap(s => (s.loaded || []).map(m => ({...m, service: s.kind})));
  const unknown = j.servers.filter(s => s.loaded == null).map(s => s.kind);
  $('#llmLoaded').innerHTML = (unknown.length ? `<p class="sub1">${t('llm.unknown_services', {services: esc(unknown.join(', '))})}</p>` : '') + (loaded.length ? `<table><thead><tr><th>${t('tab.models')}</th><th>${t('llm.reported_memory')}</th><th>${t('llm.runtime_context')}</th><th></th></tr></thead><tbody>${loaded.map(m => `<tr><td>${esc(m.name)} <span class="tag">${esc(m.service)}</span></td><td>${m.size_vram == null ? '—' : llmBytes(m.size_vram)}</td><td>${m.context == null ? '—' : m.context.toLocaleString(LANG)}</td><td>${m.service === 'Ollama' ? `<button class="small" data-unload="${esc(m.name)}" ${hw.llmPending.has(m.name) ? 'disabled' : ''}>${t('llm.unload')}</button>` : ''}</td></tr>`).join('')}</tbody></table>` : `<p class="sub1">${t(unknown.length ? 'llm.runtime_unknown' : 'llm.no_models_are_currently_in_memory')}</p>`);
  $('#llmLoaded').querySelectorAll('[data-unload]').forEach(b => b.onclick = () => llmAction(b, '/api/llm/unload', b.dataset.unload));
  $('#llmPullBtn').disabled = $('#llmPullName').disabled = !known;
  renderInventory(j.inventory || {}, ol, guard);
  const inst = (ol?.installed || []).filter(m => (m.capabilities || []).includes('completion'));
  const running = j.bench?.status === 'running';
  const disabled = running || guard.blocked || !known || !inst.length;
  renderBench(j, ol, cfg, inst, running, disabled);
}
async function llmAction(button, path, model, extra = {}) {
  if (hw.llmPending.has(model)) return;
  hw.llmPending.add(model); button.disabled = true;
  try { const r = await llmApi(path, {model, ...extra}); if (!r.ok) alert(r.error); }
  finally { hw.llmPending.delete(model); await pollLlm(); }
}
function renderInventory(inventory, ol, guard) {
  const sources = inventory.sources || [], rows = [];
  for (const source of sources) {
    for (const r of source.rows || []) rows.push({...r, source});
    if (!(source.rows || []).length && source.state === 'unavailable') rows.push({name: source.id, provider: source.type, state: 'unavailable', source, files: []});
  }
  // Newly pulled models remain visible before the next explicit filesystem check.
  for (const m of ol?.installed || []) if (!rows.some(r => r.source.type === 'ollama' && (r.aliases || [r.name]).includes(m.name))) rows.push({name: m.name, provider: 'Ollama', state: 'reported', files: [], source: {type: 'ollama', path: '—', storage: '—'}});
  $('#llmInstSub').textContent = t('llm.inventory_note');
  $('#llmStoresSub').textContent = inventory.scanning ? t('llm.scanning') : t('llm.metadata_only');
  $('#btnStores').disabled = !!inventory.scanning;
  hw.llmKeep = hw.llmKeep || {};
  $('#llmInstalled').innerHTML = `<table><thead><tr><th>${t('tab.models')}</th><th>${t('llm.storage')}</th><th>${t('llm.file_size')}</th><th>${t('llm.file_status')}</th><th></th></tr></thead><tbody>${rows.map(r => {
    const s = r.source, aliases = r.aliases || [r.name];
    const m = s.type === 'ollama' ? (ol?.installed || []).find(x => aliases.includes(x.name)) : null;
    const resident = m && (ol?.loaded || []).some(x => x.name === m.name);
    const status = s.state === 'unavailable' ? 'unavailable' : r.state;
    return `<tr><td>${esc(r.name)} <span class="tag">${esc(r.provider)}</span><details><summary>${t('llm.paths')}</summary><div class="sub1" style="overflow-wrap:anywhere">${esc(s.path)}${aliases.length > 1 ? '<br>' + aliases.map(esc).join(', ') : ''}${(r.files || []).map(f => '<br>' + esc(f.path)).join('')}${(r.missing || []).map(f => '<br>' + t('llm.missing_file', {path: esc(f)})).join('')}</div></details></td><td>${esc(s.storage || '—')}</td><td>${r.bytes == null ? '—' : llmBytes(r.bytes)}</td><td class="sub1">${t('llm.file_' + status)}<br>${s.checked_at ? esc(new Date(s.checked_at * 1000).toLocaleString(LANG)) : '—'}</td><td>${m ? `${resident ? `<span class="tag ok">${t('llm.loaded')}</span>` : `<select data-ka="${esc(m.name)}" style="${selStyle}">${['5m','30m','2h','-1'].map(k => `<option value="${k}" ${((hw.llmKeep[m.name] || '5m') === k) ? 'selected' : ''}>${k === '-1' ? t('llm.keep_loaded') : k}</option>`).join('')}</select> <button class="small" data-load="${esc(m.name)}" ${guard.blocked || ol.state !== 'online' || hw.llmPending.has(m.name) ? 'disabled' : ''}>${t('llm.load')}</button>`} <button class="small" data-rm="${esc(m.name)}" ${hw.llmPending.has(m.name) ? 'disabled' : ''}>${t('disk.delete')}</button>` : `<span class="sub1">${t(s.type === 'ollama_archive' ? 'llm.archive_only' : 'llm.no_service_action')}</span>`}</td></tr>`;
  }).join('')}</tbody></table>`;
  $('#llmInstalled').querySelectorAll('[data-ka]').forEach(s => s.onchange = () => { hw.llmKeep[s.dataset.ka] = s.value; });
  $('#llmInstalled').querySelectorAll('[data-load]').forEach(b => b.onclick = () => llmAction(b, '/api/llm/load', b.dataset.load, {keep_alive: hw.llmKeep[b.dataset.load] || '5m'}));
  $('#llmInstalled').querySelectorAll('[data-rm]').forEach(b => b.onclick = async () => {
    const name = b.dataset.rm;
    if (!confirm(t('llm.delete_if_other_tags_of_the', {v0: name}))) return;
    await llmAction(b, '/api/disk/action', name, {action: 'ollama_delete', name});
    await llmApi('/api/llm/stores/refresh', {}); await pollLlm();
  });
}
function renderBench(j, ol, cfg, inst, running, disabled) {
  const b = j.bench || {};
  const opts = inst.slice().sort((a, b2) => (a.size||0) - (b2.size||0)).map(m => `<option value="${esc(m.name)}" ${hw.llmBenchModel === m.name ? 'selected' : ''}>${esc(m.name)} (${llmBytes(m.size)})</option>`).join('');
  let bh = `<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><select id="llmModel" ${disabled ? 'disabled' : ''} style="${selStyle}">${opts}</select><select id="llmN" ${disabled ? 'disabled' : ''} style="${selStyle}">${[64,128,256,512].map(n => `<option value="${n}" ${hw.llmN===n?'selected':''}>${n} token</option>`).join('')}</select><select id="llmC" ${disabled ? 'disabled' : ''} style="${selStyle}">${[1,2,4,8].map(n => `<option value="${n}" ${hw.llmC===n?'selected':''}>${t("llm.concurrent", {v2: n})}</option>`).join('')}</select><button id="llmBenchBtn" class="small primary" ${disabled ? 'disabled' : ''}>${running ? t("llm.benchmarking") : t("llm.start_benchmark")}</button>${running ? `<span class="sub1">${esc(b.model)} · ${esc(b.phase || '')}</span>` : `<span class="sub1">${t("llm.this_actually_loads_the_model_when", {v0: cfg.num_parallel ?? 1})}</span>`}</div>`;
  if (b.status === 'done') bh += `<div class="stat" style="margin-top:12px"><div><b>${b.decode_tps ?? '—'}</b><span>${b.concurrency > 1 ? t("llm.total_decode_tok_s") : 'decode tok/s'}</span></div>${b.concurrency > 1 ? `<div><b>${b.per_request_tps && b.per_request_tps.length ? Math.round(b.per_request_tps.reduce((a,x)=>a+x,0)/b.per_request_tps.length) : '—'}</b><span>${t("llm.average_tok_s_per_request")}</span></div><div><b>${b.wall_s ?? '—'} s</b><span>${t("llm.wall_clock_time")}</span></div>${b.failed ? `<div><b style="color:var(--danger)">${b.failed}/${b.concurrency}</b><span>${t("llm.requests_failed")}</span></div>` : ''}` : `<div><b>${b.prefill_tps ?? '—'}</b><span>prefill tok/s</span></div><div><b>${b.load_ms != null ? (b.load_ms/1000).toFixed(1) + ' s' : '—'}</b><span>${t("llm.load")}</span></div>`}</div><div class="sub1" style="margin-top:6px">${esc(b.model)} · ${esc(b.finished)} · ${esc(b.note)}</div>`;
  if (b.status === 'error') bh += `<div class="panel notice danger" style="margin-top:10px;padding:10px 14px">${t("llm.benchmark_failed", {v0: esc(b.error)})}</div>`;
  const H = j.history || [];
  if (H.length) { const best = {}; for (const r of H) if (r.decode_tps && (r.concurrency||1) === 1 && (!best[r.model] || r.decode_tps > best[r.model])) best[r.model] = r.decode_tps;
    bh += `<details id="llmHist" style="margin-top:12px" ${hw.llmHistOpen === true ? 'open' : ''}><summary>${t("llm.history_records_server_side", {v1: H.length})}</summary><table><thead><tr><th>${t("tab.models")}</th><th style="width:110px">${t("llm.parameters_quantization")}</th><th style="width:60px">${t("llm.concurrency")}</th><th style="width:90px">decode</th><th style="width:80px">prefill</th><th style="width:60px">token</th><th style="width:150px">${t("history.time")}</th></tr></thead><tbody>` +
      H.map(r => `<tr><td class="mono">${esc(r.model)}${(ol?.installed || []).some(m => m.name === r.model) ? '' : `<span class="tag">${t(ol?.installed == null ? "llm.catalog_unknown" : "llm.history_model_absent")}</span>`}${best[r.model] === r.decode_tps ? `<span class="tag ok">${t("llm.best")}</span>` : ''}</td><td class="sub1">${[r.params, r.quant].filter(Boolean).map(esc).join(' / ') || '—'}</td><td class="sub1">${r.concurrency || 1}${r.failed ? `<span class="tag reboot" title="${t("llm.requests_failed")}">${r.failed} ✗</span>` : ''}</td><td class="mono">${r.decode_tps ?? '—'}${r.concurrency > 1 ? `<span class="sub1">${t("llm.total")}</span>` : ''}</td><td class="mono">${r.prefill_tps ?? '—'}</td><td class="sub1">${r.num_predict ?? '—'}</td><td class="sub1">${esc((r.finished||'').replace('T',' '))}</td></tr>`).join('') + `</tbody></table></details>`; }
  $('#llmBench').innerHTML = bh;
  const hd = $('#llmHist'); if (hd) hd.ontoggle = () => { hw.llmHistOpen = hd.open; };
  const sel = $('#llmModel'); if (sel) sel.onchange = () => { hw.llmBenchModel = sel.value; };
  const seln = $('#llmN'); if (seln) seln.onchange = () => { hw.llmN = +seln.value; };
  const selc = $('#llmC'); if (selc) selc.onchange = () => { hw.llmC = +selc.value; };
  const btn = $('#llmBenchBtn'); if (btn) btn.onclick = async () => { if (disabled) return; const model = $('#llmModel').value; hw.llmBenchModel = model; hw.llmN = +$('#llmN').value; hw.llmC = +$('#llmC').value; btn.disabled = true;
    const r = hw.llmC > 1 ? await llmApi('/api/llm/bench_concurrent', {model, concurrency: hw.llmC, num_predict: hw.llmN}) : await llmApi('/api/llm/bench', {model, num_predict: hw.llmN});
    if (!r.ok) alert(t("updates.could_not_start_with_value", {value: r.error})); await pollLlm(); };
}

$('#llmPullBtn').onclick = async () => {
  const name = $('#llmPullName').value.trim();
  if (!name || !confirm(t('llm.pull_it_will_download_directly_to', {v0: name}))) return;
  const r = await llmApi('/api/llm/pull', {model: name});
  if (!r.ok) { alert(r.error); return; }
  startPolling(`ollama pull ${name}`); window.scrollTo({top: 0, behavior: 'smooth'});
};
$('#btnStores').onclick = async () => {
  $('#btnStores').disabled = true;
  const r = await llmApi('/api/llm/stores/refresh', {});
  if (!r.ok) alert(r.error);
  await pollLlm();
};
