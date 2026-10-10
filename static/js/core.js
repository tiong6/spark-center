const $ = s => document.querySelector(s);
const state = { items: [], selected: new Set(), job: null, polling: null, apps: null, src: 'all', q: '' };
const TITLES = { monitor: t("tab.monitor"), llm: t("tab.models_llm"), updates: t("tab.updates"), apps: t("tab.apps"), disk: t("tab.disk"), hardware: t("tab.hardware"), history: t("tab.history"), faq: t("tab.faq"), setup: t("tab.setup") };
const DEFAULT_TAB = 'monitor';
const fmtBytes = b => b == null ? '—' : b < 1048576 ? (b/1024).toFixed(0)+' KB' : b < 1073741824 ? (b/1048576).toFixed(1)+' MB' : (b/1073741824).toFixed(2)+' GB';
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function api(path, body) {
  const r = await fetch(path + (path.includes('?') ? '&' : '?') + 'lang=' + encodeURIComponent(LANG), body ? {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)} : {});
  const j = await r.json().catch(() => ({ok:false, error:`HTTP ${r.status}`}));
  if (!r.ok && j.ok !== false) j.ok = false;
  return j;
}

/* 外部連結交給系統預設瀏覽器（後端 /api/open → xdg-open）。在 PWA 視窗裡 target=_blank 會在 Spark Center 自己的
   Chrome profile 開一個完整瀏覽器，隔離就漏了。後端不行（唯讀、沒桌面、服務掛了）就退回原本的新分頁，功能不斷。
   只在 App 視窗（PWA／--app 的 standalone 顯示模式）攔：一般瀏覽器分頁、SSH 轉埠的遠端使用者，新分頁本來就開在
   他自己的瀏覽器，攔了反而會把頁面開到 GX10 的螢幕上。修飾鍵（Ctrl／Shift／中鍵）照瀏覽器原意，不攔。 */
const IN_APP_WINDOW = matchMedia('(display-mode: standalone), (display-mode: window-controls-overlay), (display-mode: minimal-ui)').matches;
document.addEventListener('click', e => {
  if (!IN_APP_WINDOW) return;
  const a = e.target.closest('a[href]');
  if (!a || e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
  let u; try { u = new URL(a.getAttribute('href'), location.href); } catch (_) { return; }
  if (!/^https?:$/.test(u.protocol) || u.origin === location.origin) return;
  e.preventDefault();
  const fallback = () => window.open(u.href, '_blank', 'noopener');
  fetch('/api/open', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url: u.href})})
    .then(r => r.json()).then(j => { if (!j || !j.ok) fallback(); }).catch(fallback);
}, true);

function showTab(t) {
  document.querySelectorAll('nav.tabs a').forEach(a => a.classList.toggle('active', a.dataset.tab === t));
  for (const k of Object.keys(TITLES)) $('#tab-'+k).classList.toggle('hide', k !== t);
  $('#pageTitle').textContent = TITLES[t];
  history.replaceState(null, '', '#' + t);
  try { localStorage.setItem('spark-center.lastTab', t); } catch (e) {}
  if (t === 'apps' && !state.apps) loadApps();
  if (t === 'history') loadHistory();
  if (t === 'faq') loadFaq();
  if (t === 'setup') loadSetup();
  if (t === 'monitor') startMon(); else stopHw();
  if (t === 'hardware') showHardware();
  if (t === 'disk') loadDisk(); else if (hw.diskTimer) { clearInterval(hw.diskTimer); hw.diskTimer = null; }
  if (t === 'llm') startLlm(); else stopLlm();
}
document.querySelectorAll('nav.tabs a').forEach(a => a.onclick = () => showTab(a.dataset.tab));

