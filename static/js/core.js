const $ = s => document.querySelector(s);
const state = { items: [], selected: new Set(), job: null, polling: null, apps: null, src: 'all', q: '' };
const TITLES = { monitor: t("tab.monitor"), llm: t("tab.models_llm"), updates: t("tab.updates"), apps: t("tab.apps"), disk: t("tab.disk"), hardware: t("tab.hardware"), history: t("tab.history") };
const DEFAULT_TAB = 'monitor';
const fmtBytes = b => b == null ? '—' : b < 1048576 ? (b/1024).toFixed(0)+' KB' : b < 1073741824 ? (b/1048576).toFixed(1)+' MB' : (b/1073741824).toFixed(2)+' GB';
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function api(path, body) {
  const r = await fetch(path + (path.includes('?') ? '&' : '?') + 'lang=' + encodeURIComponent(LANG), body ? {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)} : {});
  const j = await r.json().catch(() => ({ok:false, error:`HTTP ${r.status}`}));
  if (!r.ok && j.ok !== false) j.ok = false;
  return j;
}

function showTab(t) {
  document.querySelectorAll('nav.tabs a').forEach(a => a.classList.toggle('active', a.dataset.tab === t));
  for (const k of Object.keys(TITLES)) $('#tab-'+k).classList.toggle('hide', k !== t);
  $('#pageTitle').textContent = TITLES[t];
  history.replaceState(null, '', '#' + t);
  try { localStorage.setItem('spark-center.lastTab', t); } catch (e) {}
  if (t === 'apps' && !state.apps) loadApps();
  if (t === 'history') loadHistory();
  if (t === 'monitor') startMon(); else stopHw();
  if (t === 'hardware') showHardware();
  if (t === 'disk') loadDisk(); else if (hw.diskTimer) { clearInterval(hw.diskTimer); hw.diskTimer = null; }
  if (t === 'llm') { pollLlm(); hw.llmTimer = setInterval(pollLlm, 10000); loadStores(); } else if (hw.llmTimer) { clearInterval(hw.llmTimer); hw.llmTimer = null; }
}
document.querySelectorAll('nav.tabs a').forEach(a => a.onclick = () => showTab(a.dataset.tab));

