/* 登入時自動開啟：狀態以 ~/.config/autostart 裡有沒有檔案為準，不另存設定 */
async function loadAutostart() {
  const r = await api('/api/autostart');
  const cb = $('#autostart');
  if (!r.ok) { cb.disabled = true; $('#autostartLabel').textContent = t("common.open_at_login_could_not_load"); return; }
  cb.checked = r.enabled;
}
$('#autostart').onchange = async () => {
  const cb = $('#autostart'); cb.disabled = true;
  const r = await api('/api/autostart', {enabled: cb.checked});
  cb.disabled = false;
  if (!r.ok) { alert(r.error || t("common.setting_failed")); }
  cb.checked = !!r.enabled;   // 以伺服器回報的實際狀態為準
};
api('/api/machine').then(r => { if (!r.ok) return; if (r.readonly) { document.body.classList.add('ro'); $('#roBadge').classList.remove('hide'); } const name = [r.vendor, r.product].filter(Boolean).join(' ') || r.family; $('#machineName').textContent = name; if (name) document.title = `Spark Center · ${name}`; });   // 機型讀 DMI，讀不到就留空
api('/api/dashboard').then(r => { const a = $('#dashLink'); if (r.ok && r.installed) { a.href = r.url; a.classList.remove('hide'); } });   // 沒裝 dgx-dashboard 就不顯示
(async () => {
  loadAutostart();
  await load();
  const j = await api('/api/job');
  if (j.status === 'running') startPolling(jobTitle(j));
  const t = location.hash.replace('#','');
  let last = null; try { last = localStorage.getItem('spark-center.lastTab'); } catch (e) {}
  showTab(TITLES[t] ? t : (TITLES[last] ? last : DEFAULT_TAB));   // 網址有指定就照網址，否則回到上次分頁，都沒有就監控
  api('/api/hardware/live').then(r => { if (r.ok) renderGpuAlert(r.gpu_alert); if (r.ok && r.disk_root) $('#dotDisk').classList.toggle('hide', (1 - r.disk_root.free / r.disk_root.total) < 0.9); });
  api('/api/apps').then(r => { if (r.ok) { state.apps = r; $('#dotApps').classList.toggle('hide', !r.items.some(a => a.candidate)); if (!$('#tab-apps').classList.contains('hide')) renderApps(); } });
})();
