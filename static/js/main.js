/* 登入時自動開啟：開關搬到設定分頁（static/js/setup.js），狀態仍以 ~/.config/autostart 裡有沒有檔案為準 */
api('/api/self').then(r => { if (r.ok && r.git && r.behind > 0) $('#selfBadge').classList.remove('hide'); });   // 頂欄「Spark Center 有新版」，每 6 小時 fetch 一次
api('/api/machine').then(r => { if (!r.ok) return; if (r.readonly) { document.body.classList.add('ro'); $('#roBadge').classList.remove('hide'); if (state.release) renderReleasePrompt(state.release); } const name = [r.vendor, r.product].filter(Boolean).join(' ') || r.family; $('#machineName').textContent = name; if (name) document.title = `Spark Center · ${name}`; });   // 機型讀 DMI，讀不到就留空
api('/api/dashboard').then(r => { const a = $('#dashLink'); if (r.ok && r.installed) { a.href = r.url; a.classList.remove('hide'); } });   // 沒裝 dgx-dashboard 就不顯示
(async () => {
  await load();
  const j = await api('/api/job');
  if (j.status === 'running') startPolling(jobTitle(j));
  const [t, sub] = location.hash.replace('#','').split('/');
  let last = null; try { last = localStorage.getItem('spark-center.lastTab'); } catch (e) {}
  showTab(TITLES[t] ? t : (TITLES[last] ? last : DEFAULT_TAB));   // 網址有指定就照網址，否則回到上次分頁，都沒有就監控
  if (t === 'faq' && sub) openFaq(decodeURIComponent(sub));   // #faq/<題目 id> 直接開到那一題
  api('/api/hardware/live').then(r => { if (r.ok) renderGpuAlert(r.gpu_alert); if (r.ok && r.disk_root) $('#dotDisk').classList.toggle('hide', (1 - r.disk_root.free / r.disk_root.total) < 0.9); });
  api('/api/faq/source').then(renderFaqDot);
  aiDotCheck();   // AI 布告欄：上次看過之後有新模型就亮點（只算主要供應商）
  api('/api/setup').then(j => { if (j.ok) $('#dotSetup').classList.toggle('hide', !j.items.some(i => i.recommended && !i.enabled)); });   // 設定分頁：有「建議裝但沒裝」的才亮點   // NVIDIA 改版原文時常見問題分頁亮點；伺服器一天只真的問論壇一次
  api('/api/apps').then(r => { if (r.ok) { state.apps = r; $('#dotApps').classList.toggle('hide', !r.items.some(a => a.candidate)); if (!$('#tab-apps').classList.contains('hide')) renderApps(); } });
})();
// 手動改網址列的 #分頁 或 #faq/<id> 也要跟著走（showTab 用 replaceState，不會觸發這裡）
$('#setupLink').onclick = e => { e.preventDefault(); showTab('setup'); };
/* 結束：後端以結束碼 0 退出（Restart=on-failure 不會拉回來）；有工作在跑時後端拒絕並說明 */
$('#quitLink').onclick = async e => {
  e.preventDefault();
  if (!confirm(t("quit.confirm"))) return;
  let r;
  try { r = await api('/api/quit', {}); } catch (err) { r = {ok: false, error: String((err && err.message) || err)}; }
  if (!r.ok) { alert(r.error); return; }
  document.body.innerHTML = `<div style="padding:48px 24px;font-size:15px">${esc(t("quit.done"))}</div>`;
  window.close();   // App 視窗通常允許；不允許就留著上面那行字
};
window.addEventListener('hashchange', () => { const [t, sub] = location.hash.replace('#','').split('/'); if (t === 'faq' && sub) openFaq(decodeURIComponent(sub)); else if (TITLES[t]) showTab(t); });
