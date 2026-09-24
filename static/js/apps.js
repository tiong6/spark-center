async function loadApps(force) {
  $('#apps').innerHTML = `<div class="empty">${t("apps.scanning_snap_and_flatpak_query_their")}</div>`;
  const j = await api('/api/apps' + (force ? '?force=1' : ''));
  if (!j.ok) { $('#apps').innerHTML = `<div class="panel notice danger">${t("apps.could_not_load", {v0: esc(j.error)})}</div>`; return; }
  state.apps = j; renderApps();
}
function renderApps() {
  const j = state.apps; const s = j.sources;
  const upd = j.items.filter(a => a.candidate).length;
  $('#appsMeta').textContent = t("apps.apt_snap_flatpak_updates_available_scanned", {v0: s.apt.count, v1: s.snap.ok ? s.snap.count : '—', v2: s.flatpak.ok ? s.flatpak.count : '—', v3: upd, v4: j.generated.replace('T',' ')});
  $('#dotApps').classList.toggle('hide', upd === 0);
  const notes = [];
  if (!s.snap.ok) notes.push(t("apps.snap_command_failed_snap_apps_are"));
  if (!s.flatpak.ok) notes.push(t("apps.flatpak_command_failed_flatpak_apps_are"));
  const uncheck = [...new Set(j.items.filter(a => a.source !== 'apt' && a.checked_updates === false).map(a => a.source))];
  if (uncheck.length) notes.push(t("apps.could_not_query_the_store_for", {v0: uncheck.join('、')}));
  notes.push(t("apps.only_apps_with_desktop_launchers_desktop"));
  $('#appsNote').textContent = notes.join(' ');
  const q = state.q.trim().toLowerCase();
  const rows = j.items.filter(a => (state.src === 'all' || (state.src === 'upd' ? !!a.candidate : a.source === state.src)) &&
    (!q || [a.name, a.name_zh, a.id, a.comment].some(x => (x||'').toLowerCase().includes(q))));
  if (!rows.length) { $('#apps').innerHTML = `<div class="empty">${t("apps.no_matching_apps")}</div>`; return; }
  let html = `<table><thead><tr><th style="width:34%">${t("tab.apps")}</th><th style="width:12%">${t("updates.version")}</th><th class="col-opt">${t("apps.source")}</th><th style="width:84px">${t("updates.size")}</th><th style="width:142px">${t("apps.installed_at")}</th><th style="width:110px">${t("updates.status")}</th><th style="width:124px"></th></tr></thead><tbody>`;
  const initial = a => esc((a.name_zh || a.name || '?').trim().charAt(0).toUpperCase());
  for (const a of rows) {
    const status = a.candidate ? `<span class="to mono">→ ${esc(a.candidate)}</span>` :
      (a.source !== 'apt' && a.checked_updates === false) ? `<span class="tag">${t("apps.could_not_query")}</span>` : `<span class="tag ok">${t("updates.up_to_date")}</span>`;
    const ic = a.icon_url ? `<img class="appic" src="${esc(a.icon_url)}" alt="" loading="lazy" onerror="this.outerHTML='<div class=\'appic none\'>${initial(a)}</div>'">` : `<div class="appic none" title="${t("apps.icon_unavailable", {v0: a.icon ? '：' + esc(a.icon) : ''})}">${initial(a)}</div>`;
    html += `<tr>
      <td><div class="appcell">${ic}<div class="apptext"><b>${esc(a.name_zh || a.name)}</b>${a.name_zh ? `<span class="sub1"> ${esc(a.name)}</span>` : ''}<div class="sub1 mono" title="${esc(a.id)}">${esc(a.id)}<span class="tag ${a.source}">${a.source}</span></div>${a.comment ? `<div class="sub1" title="${esc(a.comment)}">${esc(a.comment)}</div>` : ''}</div></div></td>
      <td class="mono">${esc(a.version)}</td>
      <td class="col-opt sub1">${esc(a.origin)}</td>
      <td class="sub1">${a.size ? fmtBytes(a.size) : '—'}</td>
      <td class="sub1 mono" style="font-size:12px">${a.installed_at ? esc(a.installed_at.replace('T', ' ')) : '—'}</td>
      <td>${status}</td>
      <td>${a.candidate ? `<div class="rowbtns"><button class="small" data-cl="${a.source}" data-id="${esc(a.id)}" data-inst="${esc(a.version)}">${t("updates.details")}</button>${a.source !== 'apt' ? `<button class="small primary" data-upd="${a.source}" data-id="${esc(a.id)}">${t("common.update")}</button>` : ''}</div>` : ''}</td></tr>`;
  }
  $('#apps').innerHTML = html + `</tbody></table>`;
  $('#apps').querySelectorAll('button[data-cl]').forEach(b => b.onclick = () => toggleChangelog(b, b.dataset.cl, b.dataset.id, b.dataset.inst));
  $('#apps').querySelectorAll('button[data-upd]').forEach(b => b.onclick = async () => {
    b.disabled = true;
    const r = await api('/api/apps/update', {source: b.dataset.upd, ids: [b.dataset.id]});
    if (!r.ok) { alert(t("updates.could_not_start_with_value", {value: r.error})); b.disabled = false; return; }
    startPolling(t("apps.update", {v0: b.dataset.upd, v1: b.dataset.id}) + (b.dataset.upd === 'snap' ? t("apps.a_polkit_password_dialog_will_appear") : ''));
    window.scrollTo({top: 0, behavior: 'smooth'});
  });
}
$('#appsNote') && $('#appsNote').addEventListener('click', () => {});
$('#srcChips').querySelectorAll('.chip').forEach(c => c.onclick = () => { state.src = c.dataset.src; $('#srcChips').querySelectorAll('.chip').forEach(x => x.classList.toggle('active', x === c)); if (state.apps) renderApps(); });
$('#appSearch').oninput = e => { state.q = e.target.value; if (state.apps) renderApps(); };
$('#btnAppsReload').onclick = () => loadApps(true);

