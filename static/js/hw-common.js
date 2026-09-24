/* ---- 硬體分頁 ---- */
const BT_KIND = { 'input-mouse': t("common.mouse"), 'input-keyboard': t("common.keyboard"), 'input-gaming': t("common.game_controller"), 'input-tablet': t("common.graphics_tablet"), 'audio-card': t("common.speakers"), 'audio-headphones': t("common.headphones"), 'audio-headset': t("common.headset"), 'phone': t("common.phone"), 'computer': t("common.computer"), 'printer': t("common.printer"), 'camera-photo': t("common.camera"), 'video-display': t("common.monitor") };
const GB = b => b == null ? '—' : (b / 1073741824).toFixed(1) + ' GB';
const dash = v => (v === null || v === undefined || v === '') ? '—' : esc(v);
const hw = { static: null, timer: null };
function meter(pct, warnAt=80, hotAt=92) {
  if (pct == null) return '';
  const cls = pct >= hotAt ? 'hot' : pct >= warnAt ? 'warn' : '';
  return `<div class="meter ${cls}"><i style="width:${Math.min(100, Math.max(0, pct))}%"></i></div>`;
}
function fmtUptime(s) { if (s == null) return '—'; const d = Math.floor(s/86400), h = Math.floor(s%86400/3600), m = Math.floor(s%3600/60); return (d?t("common.days", {v0: d}):'') + t("common.hours_min", {v0: h, v1: m}); }
async function ensureStatic() {
  if (hw.static) return true;
  const j = await api('/api/hardware');
  if (!j.ok) { $('#hw').innerHTML = $('#mon').innerHTML = `<div class="panel notice danger">${t("apps.could_not_load", {v0: esc(j.error)})}</div>`; return false; }
  hw.static = j; return true;
}
async function startMon() {          // 監控分頁：持續輪詢
  if (!(await ensureStatic())) return;
  // 等 /api/hardware 回來的期間使用者可能已切到別的分頁；那時不能再動計時器，否則會關掉新分頁的輪詢、開監控的
  if ($('#tab-monitor').classList.contains('hide')) return;
  stopHw(); pollHw(); hw.timer = setInterval(pollHw, MON_INTERVAL_MS);
  pollWifiExtra(); hw.wifiTimer = setInterval(pollWifiExtra, 10000);
}
async function pollWifiExtra() { const r = await api('/api/hardware/wifi'); if (r.ok) hw.wifiExtra = r; }
