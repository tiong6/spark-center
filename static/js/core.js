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

/* 共用 ⓘ：把「來源／怎麼判定／為什麼」收起來，第一眼只看資料與結論。
   標記：infoBtn(key, vars, after) → <button class="info" data-info="<i18n key>">ⓘ</button>；內容就是 t(key)（同一個 key，不另複製字串），
   可用 faqRich 的輕量標記，裡面的 data-faq 連結照常跳 FAQ。after 是額外接在後面的純文字（例如伺服器回來的 note）。
   行為：滑過開（移開就關）、點一下釘住（觸控／鍵盤走這條）、再點／Esc／點外面關；同時只開一個。
   面板絕對定位在 ⓘ 下方，靠右邊緣往左翻、靠底往上翻；面板所在區塊重畫（每 2 秒的後面板）時改認同 key 的新按鈕，找不到就關。 */
const infoBtn = (key, vars, after) => `<button type="button" class="info" data-info="${esc(key)}"${vars ? ` data-info-vars="${esc(JSON.stringify(vars))}"` : ''}${after ? ` data-info-after="${esc(after)}"` : ''} aria-label="${esc(t("common.info"))}" aria-expanded="false">ⓘ</button>`;
const infopop = { el: null, btn: null, pinned: false, timer: null, watch: null, hoverLock: null };
function infoText(btn) {
  let vars = null; try { vars = btn.dataset.infoVars ? JSON.parse(btn.dataset.infoVars) : null; } catch (e) {}
  const s = t(btn.dataset.info, vars) + (btn.dataset.infoAfter ? '\n' + btn.dataset.infoAfter : '');
  return typeof faqRich === 'function' ? faqRich(s) : `<p>${esc(s)}</p>`;   // faq.js 在 core.js 之後載入，呼叫時才查
}
function infoPlace() {
  const { el, btn } = infopop; if (!el || !btn) return;
  const r = btn.getBoundingClientRect(), vw = document.documentElement.clientWidth, vh = window.innerHeight;
  el.style.left = '0px'; el.style.top = '0px';
  const w = el.offsetWidth, h = el.offsetHeight;
  let left = r.left; if (left + w > vw - 12) left = Math.max(12, r.right - w);
  let top = r.bottom + 6; if (top + h > vh - 12 && r.top - h - 6 > 12) top = r.top - h - 6;
  el.style.left = Math.round(left + window.scrollX) + 'px'; el.style.top = Math.round(top + window.scrollY) + 'px';
}
function infoOpen(btn, pinned) {
  clearTimeout(infopop.timer);
  if (infopop.btn && infopop.btn !== btn) infoClose();
  if (!infopop.el) { infopop.el = document.createElement('div'); infopop.el.className = 'infopop'; infopop.el.setAttribute('role', 'tooltip'); document.body.appendChild(infopop.el); }
  if (infopop.btn !== btn) { infopop.el.innerHTML = infoText(btn); infopop.btn = btn; btn.classList.add('open'); btn.setAttribute('aria-expanded', 'true'); }
  infopop.pinned = infopop.pinned || !!pinned;
  infoPlace();
  if (!infopop.watch) infopop.watch = setInterval(() => {   // 按鈕被重畫掉：改認同一個 key 的新按鈕；沒有就關
    const b = infopop.btn; if (!b || document.contains(b)) return;
    const nb = [...document.querySelectorAll(`.info[data-info="${b.dataset.info.replace(/"/g, '\\"')}"]`)].find(x => !x.closest('.hide'));
    if (!nb) return infoClose();
    infopop.btn = nb; nb.classList.add('open'); nb.setAttribute('aria-expanded', 'true'); infoPlace();
  }, 500);
}
function infoClose() {
  clearTimeout(infopop.timer); clearInterval(infopop.watch); infopop.watch = null;
  // 關掉後鎖住同一個 key 的「滑過即開」，直到滑鼠真的移動：監控／後面板每 2 秒重畫，按鈕在停住的游標底下被換掉時瀏覽器會補發 mouseover，不鎖就會 Esc 完又自己開回來
  if (infopop.btn) { infopop.hoverLock = infopop.btn.dataset.info; infopop.btn.classList.remove('open'); infopop.btn.setAttribute('aria-expanded', 'false'); }
  if (infopop.el) infopop.el.remove();
  infopop.el = null; infopop.btn = null; infopop.pinned = false;
}
document.addEventListener('click', e => {
  const b = e.target.closest('.info[data-info]');
  if (b) { e.preventDefault(); if (infopop.btn === b && infopop.pinned) infoClose(); else infoOpen(b, true); return; }
  if (infopop.el && !e.target.closest('.infopop')) infoClose();
});
document.addEventListener('mouseover', e => {
  if (e.target.closest('.infopop')) { clearTimeout(infopop.timer); return; }
  const b = e.target.closest('.info[data-info]');
  if (!b || b.dataset.info === infopop.hoverLock) return;
  if (infopop.btn === b) clearTimeout(infopop.timer); else infoOpen(b, false);
});
document.addEventListener('mousemove', e => {   // 真的移動了才解鎖；若此刻就在某顆 ⓘ 上，補開
  if (infopop.hoverLock == null) return;
  infopop.hoverLock = null;
  const b = e.target.closest('.info[data-info]'); if (b && !infopop.el) infoOpen(b, false);
});
document.addEventListener('mouseout', e => {
  if (infopop.pinned || !infopop.el) return;
  if (!e.target.closest('.info[data-info], .infopop')) return;
  const to = e.relatedTarget;
  if (to && (to.closest('.infopop') || to.closest('.info[data-info]') === infopop.btn)) return;
  clearTimeout(infopop.timer); infopop.timer = setTimeout(infoClose, 220);
});
document.addEventListener('keydown', e => { if (e.key === 'Escape' && infopop.el) { infoClose(); } });
window.addEventListener('resize', infoClose);

/* Ubuntu 版本升級提示（release_prompt.status()）的共用片段：更新分頁的開關與常見問題的本機框都用，放這裡兩邊才不會各寫一份 */
function releaseNote(rp) {   // 生效值的依據：檔案不存在／讀不到／沒有 Prompt 行／寫的值和生效值不同
  if (!rp.exists) return t("release.note_missing");
  if (!rp.readable) return t("release.note_unreadable", {error: esc(rp.error || '—')});
  if (rp.default_used) return t("release.note_default");
  if (rp.raw != null && rp.raw !== rp.mode) return t("release.note_raw", {line: `<code>Prompt=${esc(rp.raw)}</code>`});
  return '';
}
const releaseSource = rp => t("release.source", {file: esc(rp.file), mtime: esc(rp.mtime ? rp.mtime.replace('T', ' ') : '—')});

function showTab(t) {
  infoClose();
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

