/* ---- 磁碟分頁 ---- */
// 分段條把掃描到的分類併成幾大塊；「其他」是即時已用量減掉掃到的總和（系統檔、未掃描目錄都在裡面），不是猜的
const SEG_GROUPS = [
  { key: 'models', label: t("tab.models"), color: '#76b900', cats: ['ollama', 'lmstudio'] },
  { key: 'docker', label: 'Docker', color: '#4aa3ff', cats: ['docker'] },
  { key: 'apps', label: t("disk.app_data"), color: '#c88fe0', cats: ['snap_home', 'flatpak', 'snapd'] },
  { key: 'cache', label: t("disk.caches"), color: '#f5a524', cats: ['apt_cache', 'cache', 'npm'] },
  { key: 'logs', label: t("disk.logs"), color: '#5fc9c0', cats: ['journal'] },
  { key: 'trash', label: t("disk.trash"), color: '#e08fa8', cats: ['trash', 'apt_autoremove'] },
];
// 數字下面畫一條相對於清單最大值的橫條；max 為 0 或值缺就不畫
const hbar = (bytes, max, cls='') => (bytes && max) ? `<div class="hbar ${cls}"><i style="width:${Math.max(1, bytes / max * 100).toFixed(1)}%"></i></div>` : '';
function diskSegBar(d, L) {
  const by = Object.fromEntries(d.categories.map(c => [c.key, c.bytes || 0]));
  const used = L.total - L.free;
  const segs = SEG_GROUPS.map(g => ({ ...g, bytes: g.cats.reduce((a, k) => a + (by[k] || 0), 0) })).filter(g => g.bytes > 0);
  const known = segs.reduce((a, g) => a + g.bytes, 0);
  const other = Math.max(0, used - known);
  if (other > 0) segs.push({ key: 'other', label: t("disk.other_system_not_scanned"), color: '#6b7370', bytes: other });
  const overlap = known > used ? known - used : 0;   // 分類有重疊或掃描與即時用量時間差時會發生，照實講
  const seg = (g) => `<i style="width:${(g.bytes / L.total * 100).toFixed(2)}%;background:${g.color}" title="${esc(g.label)} ${GB(g.bytes)}（${(g.bytes / L.total * 100).toFixed(1)}%）"></i>`;
  const leg = (g) => `<span><em style="background:${g.color}"></em>${esc(g.label)} <b>${GB(g.bytes)}</b></span>`;
  return `<div class="segbar">${segs.map(seg).join('')}</div>` +
    `<div class="seglegend">${segs.map(leg).join('')}<span><em style="background:#0f1110;border:1px solid var(--line)"></em>${t("disk.free")}<b>${GB(L.free)}</b></span>` +
    (overlap ? `<span class="sub1">${t("disk.category_totals_exceed_current_used_space", {v0: GB(overlap)})}</span>` : '') + `</div>`;
}
const ACT_LABEL = { apt_clean: t("job.clear_apt_cache"), apt_autoremove: 'apt autoremove', docker_prune: t("disk.remove_dangling_images"), npm_cache_clean: t("disk.clear_npm_cache"), trash_empty: t("disk.empty_trash") };
// 清理鈕圖示（16 格座標的 inline SVG）：垃圾桶、掃帚、方塊（映像）、封包
const ICONS = {
  trash: '<svg class="ic" viewBox="0 0 16 16"><path d="M2.5 4h11M6 4V2.5h4V4M4 4l.7 9.5h6.6L12 4M6.5 7v4M9.5 7v4"/></svg>',
  broom: '<svg class="ic" viewBox="0 0 16 16"><path d="M13.5 2.5 8 8M8 8 5.5 6.5 2.5 11l2.5 2.5 4.5-3zM4 10.5l2 2"/></svg>',
  box: '<svg class="ic" viewBox="0 0 16 16"><path d="M8 2 13.5 5v6L8 14 2.5 11V5z M2.5 5 8 8l5.5-3M8 8v6"/></svg>',
  pkg: '<svg class="ic" viewBox="0 0 16 16"><path d="M2.5 6.5h11v7h-11zM2.5 6.5 4 3h8l1.5 3.5M6.5 9.5h3"/></svg>',
};
const ACT_ICON = { apt_clean: 'pkg', apt_autoremove: 'pkg', docker_prune: 'box', npm_cache_clean: 'broom', trash_empty: 'trash' };
async function loadDisk(force) {
  const j = await api('/api/disk' + (force ? '?force=1' : ''));
  if (!j.ok) { $('#diskCats').innerHTML = `<div class="empty">${t("apps.could_not_load", {v0: esc(j.error)})}</div>`; return; }
  const L = j.live, used = L.total - L.free, pct = used / L.total * 100;
  $('#diskStat').innerHTML = `<div><b>${GB(used)}</b><span>${t("disk.used", {v1: pct.toFixed(1)})}</span></div><div><b>${GB(L.free)}</b><span>${t("disk.free_2")}</span></div><div><b>${GB(L.total)}</b><span>${t("disk.total")}</span></div>`;
  $('#diskMeter').innerHTML = j.data ? diskSegBar(j.data, L) : meter(pct, 85, 90);   // 明細還沒掃完就先給單色條
  const warn = $('#diskWarn'); warn.classList.toggle('hide', pct < 90); if (pct >= 90) warn.innerHTML = `<b>${t("disk.root_partition_is_used", {v0: pct.toFixed(0)})}</b>${t("disk.with_free_above_90_a_desktop", {v1: GB(L.free)})}`;
  $('#dotDisk').classList.toggle('hide', pct < 90);
  const d = j.data;
  $('#diskMeta').textContent = j.scanning ? t("disk.scanning_about_1_minute") : d ? t("disk.scanned_at_details_cached_10_min", {v0: d.scanned.replace('T',' ')}) : '';
  if (j.scanning) { if (!hw.diskTimer) hw.diskTimer = setInterval(() => loadDisk(), 5000); } else if (hw.diskTimer) { clearInterval(hw.diskTimer); hw.diskTimer = null; }
  if (!d) return;
  const cats = d.categories.slice().sort((a, b) => b.bytes - a.bytes);
  const catMax = Math.max(0, ...cats.map(c => c.bytes || 0));
  let html = `<table><thead><tr><th>${t("disk.category")}</th><th style="width:130px">${t("updates.size")}</th><th>${t("updates.details")}</th><th style="width:170px"></th></tr></thead><tbody>`;
  for (const c of cats) {
    const act = c.action ? `<button class="small" data-act="${c.action}">${ICONS[ACT_ICON[c.action]] || ''}${ACT_LABEL[c.action] || c.action}</button>` : '';
    const more = c.items.length ? `<details><summary class="sub1">${t("disk.details", {v0: c.items.length})}</summary><table style="margin-top:6px">${c.items.map(i => `<tr><td class="mono" style="font-size:12px">${esc(i.name)}${i.note ? `<span class="sub1"> · ${esc(i.note)}</span>` : ''}${i.modified ? `<span class="sub1"> · ${esc(i.modified)}</span>` : ''}</td><td style="width:90px" class="sub1">${i.bytes ? GB(i.bytes) : ''}</td><td style="width:70px">${c.key === 'ollama' ? `<button class="small" data-olrm="${esc(i.name)}">${t("disk.delete")}</button>` : ''}</td></tr>`).join('')}</table></details>` : '';
    html += `<tr><td><b>${esc(c.label)}</b>${more}</td><td class="mono">${c.bytes ? GB(c.bytes) : '—'}${hbar(c.bytes, catMax)}</td><td class="sub1">${esc(c.note)}</td><td>${act}</td></tr>`;
  }
  $('#diskCats').innerHTML = html + `</tbody></table>`;
  const homeMax = Math.max(0, ...d.home_top.map(h => h.bytes || 0)), bigMax = Math.max(0, ...d.big_files.map(b => b.bytes || 0));
  $('#diskHome').innerHTML = `<table><tbody>${d.home_top.map(h => `<tr><td class="mono">${esc(h.name)}${hbar(h.bytes, homeMax)}</td><td style="width:90px" class="sub1">${GB(h.bytes)}</td></tr>`).join('')}</tbody></table>`;
  $('#diskBig').innerHTML = d.big_files.length ? `<table><tbody>${d.big_files.map(b => `<tr><td class="mono" style="font-size:12px">${esc(b.path.replace(/^\/home\/[^/]+/, '~'))}${hbar(b.bytes, bigMax, 'warn')}</td><td style="width:80px" class="sub1">${GB(b.bytes)}</td></tr>`).join('')}</tbody></table>` : `<div class="empty">${t("disk.none")}</div>`;
  $('#diskCats').querySelectorAll('button[data-act]').forEach(b => b.onclick = async () => {
    const act = b.dataset.act; const msg = { docker_prune: t("disk.only_deletes_untagged_dangling_images_and"), trash_empty: t("disk.trash_contents_will_be_permanently_deleted"), apt_autoremove: t("disk.dependencies_no_longer_needed_including_old"), apt_clean: t("disk.delete_downloaded_deb_cache_files_will"), npm_cache_clean: t("disk.clear_npm_cache_the_next_npm") }[act] || '';
    if (!confirm(t("disk.confirm", {v0: ACT_LABEL[act], v1: msg}))) return;
    b.disabled = true;
    if (act === 'docker_prune') { const r1 = await api('/api/disk/action', {action: 'docker_prune'}); if (!r1.ok) { alert(r1.error); b.disabled = false; return; } startPolling(t("disk.docker_remove_dangling_images")); const w = setInterval(async () => { const jj = await api('/api/job'); if (jj.status !== 'running') { clearInterval(w); const r2 = await api('/api/disk/action', {action: 'docker_builder_prune'}); if (r2.ok) startPolling(t("disk.docker_clear_build_cache")); } }, 1500); return; }
    const r = await api('/api/disk/action', {action: act});
    if (!r.ok) { alert(t("updates.could_not_start_with_value", {value: r.error})); b.disabled = false; return; }
    startPolling(`${ACT_LABEL[act]}${r.packages ? '：' + r.packages.join(' ') : ''}`);
    window.scrollTo({top: 0, behavior: 'smooth'});
  });
  $('#diskCats').querySelectorAll('button[data-olrm]').forEach(b => b.onclick = async () => {
    const name = b.dataset.olrm;
    if (!confirm(t("disk.delete_ollama_model_if_other_tags", {v0: name}))) return;
    b.disabled = true; const r = await api('/api/disk/action', {action: 'ollama_delete', name});
    if (!r.ok) { alert(t("disk.failed_with_value", {value: r.error})); b.disabled = false; return; }
    loadDisk(true);
  });
}
$('#btnDiskScan').onclick = () => loadDisk(true);

