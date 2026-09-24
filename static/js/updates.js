async function toggleChangelog(btn, source, id, installed) {
  const tr = btn.closest('tr'); const next = tr.nextElementSibling;
  if (next && next.classList.contains('clrow')) { next.remove(); btn.textContent = t("updates.details"); return; }
  btn.disabled = true; btn.textContent = t("updates.loading");
  const j = await api(`/api/changelog?source=${encodeURIComponent(source)}&id=${encodeURIComponent(id)}&installed=${encodeURIComponent(installed||'')}`);
  btn.disabled = false; btn.textContent = t("updates.collapse");
  const row = document.createElement('tr'); row.className = 'clrow';
  const linkify = t => esc(t).replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener" style="color:var(--info)">$1</a>');
  let body = '';
  if (j.summary) body += `<div style="margin-bottom:4px">${esc(j.summary)}</div>`;
  if (j.meta) body += `<div class="sub1" style="margin-bottom:8px">${esc(j.meta)}</div>`;
  if (j.table) {                    // 真表格，不靠空格對齊（等寬字型下 CJK 寬度不保證是英數兩倍）
    body += `<table class="cltab"><thead><tr>${j.table.columns.map((c, i) => `<th${i >= 3 ? ' style="text-align:right"' : ''}>${esc(c)}</th>`).join('')}</tr></thead><tbody>` +
      j.table.rows.map(r => `<tr${r.installed ? ' class="me"' : ''}><td>${esc(r.channel)}${r.current ? `<span class="tag ok">${t("updates.current_version")}</span>` : ''}</td><td class="mono">${esc(r.version)}</td><td class="sub1">${esc(r.date)}</td><td class="mono" style="text-align:right">${esc(r.rev)}</td><td class="sub1" style="text-align:right">${esc(r.size)}</td></tr>`).join('') + `</tbody></table>`;
  } else if (j.text) {
    body += `<pre class="cl">${linkify(j.text)}</pre>`;
  }
  if (j.store_url) body += `<div class="sub1" style="margin-top:8px">${t("updates.store_page", {v0: linkify(j.store_url)})}</div>`;
  row.innerHTML = `<td colspan="${tr.children.length}"><div class="sub1" style="margin-bottom:6px">${linkify(j.note || j.error || '')}</div>${body}</td>`;
  tr.after(row);
}

function renderReboot(rb) {
  const el = $('#reboot');
  if (!rb || !rb.required) { el.classList.add('hide'); return; }
  el.classList.remove('hide');
  el.innerHTML = t("updates.this_tool_never_reboots_on_its_with_value", {value: `<b>${t("updates.the_system_currently_needs_a_reboot")}</b>${t("updates.based_on_var_run_reboot_required")}` +
    (rb.packages.length ? t("updates.triggered_by_packages", {v0: rb.packages.map(esc).join('、')}) : '')});
}
/* 來源群組的識別色：已知廠商固定色，其餘從色盤依名稱穩定挑一個（只做辨識，不代表重要程度） */
const GRP_PALETTE = ['#d8b45a', '#c88fe0', '#5fc9c0', '#f0a06a', '#8fb0ff', '#e08fa8'];
function groupColor(name, site) {
  const k = `${name} ${site}`.toLowerCase();
  if (k.includes('ubuntu')) return '#e95420';
  if (k.includes('nvidia')) return 'var(--accent)';
  if (k.includes('microsoft')) return '#4aa3ff';
  let h = 0; for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return GRP_PALETTE[h % GRP_PALETTE.length];
}
function renderList() {
  const box = $('#list');
  // 綠點與空狀態只看主清單：折疊起來的「本機用不到的 firmware 子套件」不算待辦
  const active = state.items.filter(it => !(it.firmware && it.firmware.passive));
  $('#dotUpd').classList.toggle('hide', active.length === 0);
  if (!state.items.length) { box.innerHTML = `<div class="empty">${t("updates.there_are_currently_no_upgradable_packages")}</div>`; updateGo(); return; }
  const THEAD = `<thead><tr><th style="width:36px"></th><th style="width:32%">${t("updates.package")}</th><th style="width:24%">${t("updates.version")}</th><th class="col-opt">${t("updates.description")}</th><th style="width:80px">${t("updates.size")}</th><th style="width:80px"></th></tr></thead>`;
  const row = it => `<tr class="sub">
        <td><input type="checkbox" data-pkg="${esc(it.name)}" ${state.selected.has(it.name)?'checked':''}></td>
        <td>${esc(it.name)}${state.newAfterRefresh && state.newAfterRefresh.has(it.name) ? `<span class="tag ok" title="${t("updates.new_after_refresh_hint")}">${t("updates.new_after_refresh")}</span>` : ''}${it.security?'<span class="tag sec">security</span>':''}${it.spark_core?`<span class="tag" style="color:#76b900;border-color:#4b6b1e" title="${t("updates.spark_os_nvidia_platform_packages_dashboard")}">${t("updates.spark_os_use_the_stock_dashboard")}</span>`:''}${it.reboot_hint?`<span class="tag reboot" title="${t("updates.based_on_the_package_name_estimated")}">${t("updates.usually_needs_reboot_estimated")}</span>`:''}${it.firmware && it.firmware.passive?`<span class="tag" title="${t("updates.none_of_the_loaded_kernel_modules", {v0: esc(it.firmware.pulled_by ? t("updates.pulled_in_as_a_dependency_of", {v0: it.firmware.pulled_by}) : t("updates.automatically_installed")), v1: it.firmware.files})}">${t("updates.no_local_driver_declares_use")}</span>`:''}</td>
        <td class="mono">${esc(it.installed)}<br><span class="to">→ ${esc(it.candidate)}</span></td>
        <td class="col-opt sub1">${esc(it.summary)}</td>
        <td class="sub1">${fmtBytes(it.size)}</td>
        <td><button class="small" data-cl="apt" data-id="${esc(it.name)}" data-inst="${esc(it.installed)}">${t("updates.details")}</button></td></tr>`;
  // 被 linux-firmware 總包拖入、本機已載入模組都用不到的 firmware 子套件：收進折疊區，不占視線（仍可勾選）
  const passive = state.items.filter(it => it.firmware && it.firmware.passive);
  const groups = {};
  for (const it of state.items) if (!passive.includes(it)) (groups[it.group] ||= []).push(it);
  let html = Object.keys(groups).length ? `<table>${THEAD}<tbody>` : '';
  for (const g of Object.keys(groups).sort()) {
    const items = groups[g]; const allSel = items.every(i => state.selected.has(i.name));
    html += `<tr class="grp" style="--grpc:${groupColor(g, items[0].site)}"><td><input type="checkbox" data-group="${esc(g)}" ${allSel?'checked':''}></td><td colspan="5"><span class="gname">${esc(g)}</span> <span class="site">${t("updates.packages", {v4: items.length, v5: esc(items[0].site), v6: items[0].origin_raw && items[0].origin_raw !== g ? ` · Origin「${esc(items[0].origin_raw)}」` : ''})}</span></td></tr>`;
    for (const it of items) html += row(it);
  }
  if (Object.keys(groups).length) html += `</tbody></table>`;
  else html += `<div class="empty">${t("updates.there_are_currently_no_updates_requiring")}</div>`;
  if (passive.length) {
    const allSel = passive.every(i => state.selected.has(i.name));
    html += `<details id="passiveFw" style="margin-top:12px" ${state.passiveOpen ? 'open' : ''}><summary class="sub1">${t("updates.firmware_sub_packages_this_machine_does", {v1: passive.length})}</summary>` +
      `<table style="margin-top:6px">${THEAD}<tbody><tr class="grp"><td><input type="checkbox" data-passive="1" ${allSel?'checked':''}></td><td colspan="5"><span class="gname" style="color:var(--muted)">${t("updates.firmware_sub_packages")}</span> <span class="site">${t("updates.packages_safe_to_upgrade_this_machine", {v2: passive.length})}</span></td></tr>${passive.map(row).join('')}</tbody></table></details>`;
  }
  box.innerHTML = html;
  const det = box.querySelector('#passiveFw'); if (det) det.ontoggle = () => { state.passiveOpen = det.open; };
  box.querySelectorAll('input[data-passive]').forEach(cb => cb.onchange = () => { for (const it of passive) cb.checked ? state.selected.add(it.name) : state.selected.delete(it.name); renderList(); });
  box.querySelectorAll('input[data-pkg]').forEach(cb => cb.onchange = () => { cb.checked ? state.selected.add(cb.dataset.pkg) : state.selected.delete(cb.dataset.pkg); renderList(); });
  box.querySelectorAll('input[data-group]').forEach(cb => cb.onchange = () => { for (const it of state.items) if (it.group === cb.dataset.group && !passive.includes(it)) cb.checked ? state.selected.add(it.name) : state.selected.delete(it.name); renderList(); });
  box.querySelectorAll('button[data-cl]').forEach(b => b.onclick = () => toggleChangelog(b, b.dataset.cl, b.dataset.id, b.dataset.inst));
  updateGo();
}
function updateGo() {
  const n = state.selected.size, running = state.job && state.job.status === 'running';
  $('#btnGo').textContent = t("updates.check_impact_and_update", {v0: n});
  $('#btnGo').disabled = n === 0 || running; $('#btnRefresh').disabled = running;
}
/* ---- 韌體（fwupd）：Dashboard 說成功不算，以 fwupd 的版本與歷史為準 ---- */
async function loadFirmware(force) {
  const j = await api('/api/firmware' + (force ? '?force=1' : ''));
  const box = $('#fw'); if (!box) return;
  if (!j.ok || !j.available) { box.innerHTML = `<div class="empty">${esc(j.error || j.note || t("updates.fwupd_unavailable"))}</div>`; return; }
  if (j.error && !j.devices.length) { box.innerHTML = `<div class="panel notice danger">${esc(j.error)}</div>`; $('#fwMeta').textContent = ''; $('#btnFwUpdate').classList.add('hide'); return; }
  // 查詢部分失敗時，數字是 null → 顯示「—」，不顯示 0；並在上方掛紅字說明
  const q = v => v == null ? '—' : v;
  $('#fwMeta').textContent = t("updates.fwupd_updatable_devices_updates_available_version", {v0: j.fwupd_version || '—', v1: j.devices.length, v2: q(j.updates), v3: q(j.mismatches), v4: q(j.pending), v5: j.generated.replace('T',' ')});
  $('#btnFwUpdate').classList.toggle('hide', !j.updates);
  const errBanner = j.error ? `<div class="panel notice danger" style="margin-bottom:10px">${esc(j.error)}</div>` : '';
  const vis = j.devices.filter(d => !d.hidden), hid = j.devices.filter(d => d.hidden);
  const row = d => { const last = d.history[0]; const st = d.mismatch ? `<span class="tag reboot">${t("updates.history_reports_success_but_version_differs")}</span>` : d.pending ? `<span class="tag sec">${t("updates.awaiting_reboot_to_apply")}</span>` : d.update_available ? `<span class="tag sec">${t("updates.new_version", {v0: esc(d.latest)})}</span>` : j.updates == null ? `<span class="tag">${t("updates.unknown_query_failed")}</span>` : `<span class="tag ok">${t("updates.up_to_date")}</span>`;
    return `<tr><td><b>${esc(d.name)}</b><div class="sub1">${esc(d.summary || d.plugin || '')}</div></td><td class="mono">${esc(d.version || '—')}</td><td>${st}</td><td class="sub1">${last ? `${esc(last.old || '?')} → ${esc(last.new || '?')}，${esc(last.state_zh)}${last.error ? '：' + esc(last.error) : ''}<br>${esc((last.when || '').replace('T',' '))}` : '—'}</td></tr>`; };
  let html = errBanner + `<table><thead><tr><th style="width:30%">${t("updates.device")}</th><th style="width:16%">${t("updates.current_version_2")}</th><th style="width:20%">${t("updates.status")}</th><th>${t("updates.last_update_fwupd_history")}</th></tr></thead><tbody>${vis.map(row).join('')}</tbody></table>`;
  if (hid.length) html += `<details style="margin-top:8px"><summary class="sub1">${t("updates.hidden_devices_certificates_and_keys", {v0: hid.length})}</summary><table><tbody>${hid.map(row).join('')}</tbody></table></details>`;
  html += `<div class="sub1" style="margin-top:8px">${t("updates.version_mismatch_means_fwupd_history_records")}</div>`;
  box.innerHTML = html;
}
$('#btnFwRefresh').onclick = async () => { $('#fwMeta').textContent = t("updates.querying_lvfs"); const r = await api('/api/disk/action', {action: 'fwupd_refresh'}); if (!r.ok) { alert(r.error); return; } startPolling(t("updates.fwupd_refresh_lvfs_metadata")); const w = setInterval(async () => { const jj = await api('/api/job'); if (jj.status !== 'running') { clearInterval(w); loadFirmware(true); } }, 1500); };
$('#btnFwUpdate').onclick = async () => { if (!confirm(t("updates.install_all_available_firmware_updates_with"))) return; const r = await api('/api/disk/action', {action: 'fwupd_update'}); if (!r.ok) { alert(r.error); return; } startPolling(t("updates.fwupd_install_firmware_updates")); window.scrollTo({top: 0, behavior: 'smooth'}); };

/* ---- npm 全域套件：Claude Code、Gemini CLI、OpenClaw 這類 CLI 工具，apt/snap/flatpak 都看不到 ---- */
async function loadNpm(force) {
  const box = $('#npm'); if (!box) return;
  const j = await api('/api/npm' + (force ? '?force=1' : ''));
  if (!j.ok || !j.available) { box.innerHTML = `<div class="empty">${esc(j.error || j.note || '—')}</div>`; $('#npmMeta').textContent = ''; $('#btnNpmUpdate').classList.add('hide'); return; }
  const q = v => v == null ? '—' : v;   // 查不到新版時顯示「—」，不顯示 0
  $('#npmMeta').textContent = t("updates.npm_meta", {n: j.packages.length, o: q(j.outdated), prefix: j.prefix || '—', when: j.generated.replace('T', ' ')});
  $('#btnNpmUpdate').classList.toggle('hide', !j.outdated);
  let html = j.error ? `<div class="panel notice danger" style="margin-bottom:10px">${esc(j.error)}</div>` : '';
  if (!j.packages.length) { box.innerHTML = html + `<div class="empty">${t("updates.npm_none")}</div>`; return; }
  html += `<table><thead><tr><th style="width:34%">${t("updates.package")}</th><th style="width:16%">${t("updates.current_version_2")}</th><th style="width:16%" title="${t("updates.npm_latest_hint")}">${t("updates.npm_latest")}</th><th>${t("updates.status")}</th><th style="width:120px"></th></tr></thead><tbody>`;
  for (const p of j.packages) {
    const st = p.outdated ? `<span class="tag sec">${t("updates.new_version", {v0: esc(p.latest)})}</span>` : p.newer ? `<span class="tag" title="${t("updates.npm_newer_hint")}">${t("updates.npm_newer", {v0: esc(p.latest)})}</span>` : j.outdated == null ? `<span class="tag">${t("updates.unknown_query_failed")}</span>` : `<span class="tag ok">${t("updates.up_to_date")}</span>`;
    // 名稱下面放描述、作者、倉庫：都是套件自己宣稱的，npm 不驗證，所以只照實顯示、附連結讓人自己看
    const repoText = p.repo ? p.repo.replace(/^https?:\/\//, '') : null;
    const who = [p.author ? esc(p.author) : null, repoText ? `<a href="${esc(p.repo)}" target="_blank" rel="noopener">${esc(repoText)}</a>` : (p.homepage ? `<a href="${esc(p.homepage)}" target="_blank" rel="noopener">${esc(p.homepage.replace(/^https?:\/\//, ''))}</a>` : null), p.license ? esc(p.license) : null].filter(Boolean).join(' · ');
    html += `<tr><td><span class="mono">${esc(p.name)}</span>${p.description ? `<div class="sub1">${esc(p.description)}</div>` : ''}<div class="sub1">${who || t("updates.npm_no_meta")}</div></td><td class="mono">${esc(p.current || '—')}</td><td class="mono">${esc(p.latest || '—')}</td><td>${st}</td><td>${p.outdated ? `<button class="small primary" data-npm="${esc(p.name)}">${t("common.update")}</button>` : p.newer ? `<button class="small" data-npm="${esc(p.name)}" title="${t("updates.npm_newer_hint")}">${t("updates.npm_install_compat", {v0: esc(p.latest)})}</button>` : ''}</td></tr>`;
  }
  box.innerHTML = html + `</tbody></table><div class="sub1" style="margin-top:8px">${t("updates.npm_trust_note")}</div>`;
  box.querySelectorAll('button[data-npm]').forEach(b => b.onclick = () => npmUpdate([b.dataset.npm], b));
  $('#btnNpmUpdate').onclick = () => npmUpdate(j.packages.filter(p => p.outdated).map(p => p.name), $('#btnNpmUpdate'));
}
async function npmUpdate(names, btn) {
  if (!confirm(t("updates.npm_confirm", {list: names.join(', ')}))) return;
  btn.disabled = true;
  const r = await api('/api/npm/update', {names});
  if (!r.ok) { alert(t("updates.could_not_start_with_value", {value: r.error})); btn.disabled = false; return; }
  startPolling(t("job.npm_update_with_value", {value: names.join(', ')})); window.scrollTo({top: 0, behavior: 'smooth'});
}
$('#btnNpmRefresh').onclick = () => { $('#npmMeta').textContent = t("updates.npm_querying"); loadNpm(true); };

/* ---- 降回上一版：清單來自 data/rollback/index.json，每筆是一次更新工作 ---- */
async function loadRollback() {
  const box = $('#rollback'); if (!box) return;
  const j = await api('/api/rollback');
  if (!j.ok) { box.innerHTML = `<div class="empty">${esc(j.error)}</div>`; return; }
  const jobs = (j.jobs || []).filter(x => (x.packages || []).length);
  if (!jobs.length) { box.innerHTML = `<div class="empty">${t("updates.rollback_none")}</div>`; return; }
  const src = { cache: t("updates.kept_from_cache"), repo: t("updates.kept_from_repo"), launchpad: t("updates.kept_from_launchpad"), npm: t("updates.kept_npm") };
  const ver = { sha256: t("updates.verified_sha256"), apt: t("updates.verified_apt"), tls: t("updates.verified_tls"), registry: t("updates.verified_registry") };
  const head = `<table><thead><tr><th style="width:28%">${t("updates.package")}</th><th style="width:26%">${t("updates.version")}</th><th>${t("updates.status")}</th><th style="width:190px"></th></tr></thead><tbody>`;
  // 面板會越留越長（5 次 × 每次好幾個套件）：只有還能降回的整筆照常顯示，已經降回或什麼都沒留的收進底下可展開的「其他紀錄」
  const keptOf = job => job.packages.filter(p => (p.deb || (p.kind === 'npm' && p.old)) && p.installed !== p.old);   // 已經是舊版的不算，按鈕沒意義
  const active = jobs.filter(j => keptOf(j).length), inactive = jobs.filter(j => !keptOf(j).length);
  let html = active.length ? head : '';
  const renderJob = job => {
    const kept = keptOf(job);
    // 一組一起降回：舊主程式要求舊函式庫時，單獨降一個 apt 會拒絕，整組給才有完整退路
    const allBtn = kept.length > 1 ? ` <button class="small" data-rb-job="${esc(job.id)}" data-rb-name="" data-rb-all="${kept.map(p => p.name).join(', ')}">${t("updates.rollback_all_btn", {n: kept.length})}</button>` : '';
    html += `<tr class="grp"><td colspan="3"><span class="gname" style="color:var(--muted)">${t("updates.updated_at")} ${esc(job.started.replace('T', ' '))}</span></td><td>${allBtn}</td></tr>`;
    for (const p of job.packages) {
      const dep = p.selected === false ? ` <span class="tag" title="${t("updates.dep_pulled_hint")}">${t("updates.dep_pulled")}</span>` : '';
      const has = p.deb || (p.kind === 'npm' && p.old);
      const atOld = has && p.installed === p.old;
      const st = has ? `<span class="tag ok">${esc(src[p.source] || p.source)}</span><span class="sub1"> ${esc(ver[p.verified] || '')}</span>` : `<span class="tag" title="${esc(p.reason || '')}">${t("updates.not_kept")}</span><span class="sub1"> ${esc(p.reason || '')}</span>`;
      const cur = atOld ? `<span class="tag">${t("updates.rollback_at_old")}</span>` : (p.installed && p.installed !== p.new ? `<span class="sub1"> ${t("updates.rollback_now_with_value", {value: esc(p.installed)})}</span>` : '');
      html += `<tr class="sub"><td class="mono" style="padding-left:20px">${esc(p.name)}${dep}</td><td class="mono sub1">${esc(p.old || '—')} → ${esc(p.new || t("updates.removed"))}${cur}</td><td>${st}</td><td>${has && !atOld ? `<button class="small" data-rb-job="${esc(job.id)}" data-rb-name="${esc(p.name)}" data-rb-old="${esc(p.old)}" data-rb-new="${esc(p.new || '')}">${t("updates.rollback_btn", {v0: esc(p.old)})}</button>` : ''}</td></tr>`;
    }
  };
  active.forEach(renderJob);
  if (active.length) html += `</tbody></table>`;
  if (inactive.length) {
    if (!active.length) html += `<div class="empty">${t("updates.rollback_nothing_active")}</div>`;
    html += `<details style="margin-top:8px"><summary class="sub1">${t("updates.rollback_other_records", {n: inactive.length})}</summary>${head}`;
    inactive.forEach(renderJob);
    html += `</tbody></table></details>`;
  }
  box.innerHTML = html;
  // 降回和更新一樣走「模擬 → 展示實際變更 → 確認」：舊函式庫可能讓 apt 想移除依賴新版的應用程式，後端遇到會拒絕，這裡照實顯示原因
  box.querySelectorAll('button[data-rb-job]').forEach(b => b.onclick = async () => {
    const all = b.dataset.rbName === '', label = all ? b.dataset.rbAll : b.dataset.rbName;
    b.disabled = true;
    const sim = await api('/api/rollback', {job: b.dataset.rbJob, name: b.dataset.rbName, simulate: true});
    b.disabled = false;
    const body = $('#mBody');
    if (!sim.ok) {
      let h = `<div class="panel notice danger">${esc(sim.error)}</div>`;
      if (sim.changes) h += `<div class="chg">` + sim.changes.map(c => `<div class="${c.action==='remove'?'rm':(c.requested?'':'extra')}">${c.action.padEnd(9)} ${esc(c.name)} ${esc(c.from)}${c.to?' → '+esc(c.to):''}</div>`).join('') + `</div>`;
      body.innerHTML = h; $('#mOk').classList.add('hide'); openModal(t("updates.cannot_proceed")); return;
    }
    const extra = sim.changes.filter(c => !c.requested);
    let html = `<p>${all ? t("updates.rollback_all_confirm", {list: esc(b.dataset.rbAll)}) : t("updates.rollback_confirm", {name: esc(b.dataset.rbName), new: esc(b.dataset.rbNew), old: esc(b.dataset.rbOld)})}</p>`;
    html += `<p>${t("updates.rollback_sim_summary", {n: sim.changes.length})}</p>`;
    if (extra.length) html += `<div class="panel notice warn">${t("updates.packages_you_did_not_select_will", {v0: extra.length})}</div>`;
    html += `<div class="chg">` + sim.changes.map(c => `<div class="${c.requested?'':'extra'}">${c.action.padEnd(9)} ${esc(c.name)} ${esc(c.from)}${c.to?' → '+esc(c.to):''}</div>`).join('') + `</div>`;
    html += `<p class="muted" style="margin-top:12px">${t("updates.rollback_conffile_note")}</p>`;
    body.innerHTML = html; $('#mOk').classList.remove('hide'); $('#mOk').textContent = t("common.confirm_rollback");
    $('#mOk').onclick = async () => {
      closeModal();
      const r = await api('/api/rollback', {job: b.dataset.rbJob, name: b.dataset.rbName});
      if (!r.ok) { alert(r.error); return; }
      startPolling(t("job.rollback_with_value", {value: label})); window.scrollTo({top: 0, behavior: 'smooth'});
    };
    openModal(t("updates.confirm_changes"));
  });
}
async function load() {
  loadFirmware(); loadNpm(); loadRollback();
  const j = await api('/api/updates');
  if (!j.ok) { $('#list').innerHTML = `<div class="panel notice danger">${t("updates.could_not_load_the_list", {v0: esc(j.error)})}</div>`; return; }
  const names = new Set(j.items.map(i => i.name));
  for (const s of [...state.selected]) if (!names.has(s)) state.selected.delete(s);
  if (state.namesBeforeRefresh) { state.newAfterRefresh = new Set([...names].filter(n => !state.namesBeforeRefresh.has(n))); state.namesBeforeRefresh = null; }   // 只在重新整理來源後標，裝完套件不重算
  else if (state.newAfterRefresh) for (const n of [...state.newAfterRefresh]) if (!names.has(n)) state.newAfterRefresh.delete(n);
  state.items = j.items;
  const nPassive = j.items.filter(i => i.firmware && i.firmware.passive).length;
  $('#meta').textContent = t("updates.upgradable_apt_index_last_updated", {v0: j.items.length - nPassive, v1: nPassive ? t("updates.another_firmware_packages_this_machine_does", {v0: nPassive}) : '', v2: j.last_refresh ? j.last_refresh.replace('T',' ') : '—'});
  const D = j.dashboard;
  $('#dashCompare').innerHTML = D ? `${t("updates.comparison")}<b>${t("updates.dgx_dashboard_s_update")}</b>${t("updates.performs_all_at_once_upgrade", {v0: D.upgrade, v1: D.install ? t("updates.newly_install", {v0: D.install}) : '', v2: D.remove ? ` · <span style="color:var(--danger)">${t("updates.remove", {v0: D.remove})}</span>` : '', v3: D.firmware != null ? t("updates.firmware", {v0: D.firmware}) : ''})}<span style="color:var(--warn)">${t("updates.forced_reboot")}</span>${t("updates.without_showing_the_list", {v4: esc(D.note)})}` : '';
  renderReboot(j.reboot); renderList();
}

$('#btnGo').onclick = async () => {
  const names = [...state.selected];
  $('#btnGo').disabled = true;
  const sim = await api('/api/simulate', {packages: names});
  $('#btnGo').disabled = false;
  const body = $('#mBody');
  if (!sim.ok) { body.innerHTML = `<div class="panel notice danger">${t("updates.could_not_simulate", {v0: esc(sim.error)})}</div>`; $('#mOk').classList.add('hide'); openModal(t("updates.cannot_proceed")); return; }
  const extra = sim.changes.filter(c => !c.requested), rm = sim.changes.filter(c => c.action === 'remove');
  let html = '';
  if (sim.pairing) html += `<div class="panel notice danger"><b>${t("updates.upgrade_the_kernel_and_nvidia_driver")}</b><div style="margin-top:6px">${esc(sim.pairing.message)}</div><div class="sub1" style="margin-top:6px">${t("updates.missing")}<span class="mono">${sim.pairing.missing.map(esc).join('、')}</span></div><div style="margin-top:8px"><button class="small primary" id="pairAdd">${t("updates.select_them_too_and_check_again")}</button></div></div>`;
  html += `<p>${t("updates.you_selected")}<b>${names.length}</b>${t("updates.packages_apt_will_actually_change")}<b>${sim.changes.length}</b>${t("updates.packages_download_approximately_disk_space_change", {v2: fmtBytes(sim.download_bytes), v3: fmtBytes(Math.abs(sim.space_bytes)), v4: sim.space_bytes<0?t("updates.freed"):''})}</p>`;
  if (rm.length) html += `<div class="panel notice danger">${t("updates.this_will")}<b>${t("updates.remove_2")}</b>${t("updates.packages_please_confirm_this_is_what", {v0: rm.length})}</div>`;
  if (extra.length) html += `<div class="panel notice warn">${t("updates.packages_you_did_not_select_will", {v0: extra.length})}</div>`;
  html += `<div class="chg">` + sim.changes.map(c => `<div class="${c.action==='remove'?'rm':(c.requested?'':'extra')}">${c.action.padEnd(9)} ${esc(c.name)} ${esc(c.from)}${c.to?' → '+esc(c.to):''}</div>`).join('') + `</div>`;
  html += `<p class="muted" style="margin-top:12px">${t("updates.after_confirmation_a_polkit_password_dialog")}<b>${t("updates.not")}</b>${t("updates.reboot_automatically")}</p>`;
  body.innerHTML = html; $('#mOk').classList.remove('hide'); $('#mOk').textContent = t("common.confirm_update");
  const pa = $('#pairAdd');
  if (pa) pa.onclick = () => { sim.pairing.missing.forEach(n => state.selected.add(n)); closeModal(); renderList(); $('#btnGo').click(); };
  $('#mOk').onclick = async () => {
    closeModal();
    const r = await api('/api/install', {packages: names});
    if (!r.ok) { alert(t("updates.could_not_start_with_value", {value: r.error})); return; }
    state.selected.clear(); startPolling(t("updates.updating_with_value", {value: names.join(', ')}));
  };
  openModal(t("updates.confirm_changes"));
};
$('#btnRefresh').onclick = async () => { state.namesBeforeRefresh = new Set(state.items.map(i => i.name)); const r = await api('/api/refresh', {}); if (!r.ok) { alert(t("updates.could_not_start_with_value", {value: r.error})); return; } startPolling(t("job.refresh_apt_sources")); };
// 全選只選主清單；折疊區的 firmware 子套件要升級就展開用它自己的群組勾選框
$('#btnAll').onclick = () => { state.items.filter(i => !(i.firmware && i.firmware.passive)).forEach(i => state.selected.add(i.name)); renderList(); };
$('#btnNone').onclick = () => { state.selected.clear(); renderList(); };
function openModal(t) { $('#mTitle').textContent = t; $('#overlay').classList.add('open'); }
function closeModal() { $('#overlay').classList.remove('open'); }
$('#mCancel').onclick = closeModal;

function jobTitle(j) {   // 重新整理頁面或服務重啟後接回工作時，標題要對得上實際在做的事
  const pk = (j.packages || []).join(', ');
  return ({ refresh: t("job.refresh_apt_sources"), install: t("job.apt_install_with_value", {value: pk}), remove: t("job.apt_remove_with_value", {value: pk}),
            aptclean: t("job.clear_apt_cache"), snap: t("job.snap_update_with_value", {value: pk}), flatpak: t("job.flatpak_update_with_value", {value: pk}),
            ollama_pull: 'ollama pull ' + pk, shell: t("job.system_action_with_value", {value: pk}),
            npm: t("job.npm_update_with_value", {value: pk}),
            rollback: t("job.rollback_with_value", {value: (j.packages || [])[1] || (j.packages || [])[0] || pk}),
            rollback_npm: t("job.rollback_with_value", {value: (j.packages || [])[1] || (j.packages || [])[0] || pk}) })[j.kind] || (t("job.job_with_value", {value: pk}));
}
function startPolling(title) {
  const c = $('#jobcard'); c.className = 'panel notice';
  $('#jobtitle').textContent = title; $('#jobactions').innerHTML = '';
  clearInterval(state.polling); state.polling = setInterval(pollJob, 1000); pollJob();
}
async function pollJob() {
  const j = await api('/api/job'); state.job = j; updateGo();
  if (j.status === 'idle' && state.polling) {
    // 工作狀態只存在服務的記憶體：服務重啟後這裡會一直等一個不存在的工作，看起來像「停住」。要明講，並重讀清單看實際結果。
    clearInterval(state.polling); state.polling = null;
    $('#jobcard').className = 'panel notice warn';
    $('#jobstatus').textContent = t("job.lost");
    $('#jobactions').innerHTML = `<button id="jobClose">${t("job.close")}</button>`;
    $('#jobClose').onclick = () => $('#jobcard').classList.add('hide');
    load(); return;
  }
  if (j.progress == null) $('#jobprog').removeAttribute('value'); else $('#jobprog').value = j.progress;
  const x = j.status === 'running' && j.xfer && j.xfer.total ? j.xfer : null;
  const xferText = x ? t("job.downloaded_mb", {v0: (x.done/1e6).toFixed(1), v1: (x.total/1e6).toFixed(1)}) + (x.speed ? ` · ${(x.speed/1e6).toFixed(1)} MB/s` : '') + (x.eta > 0 ? t("job.about_remaining", {v0: x.eta >= 60 ? t("job.min_with_value", {value: Math.round(x.eta/60)}) : t("job.sec_with_value", {value: x.eta})}) : '') : '';
  $('#jobstatus').textContent = (j.status_text || '') + (j.details ? ' · ' + j.details : '') + (j.status==='running' && j.progress != null ? ` · ${j.progress}%` : '') + xferText;
  $('#joblog').textContent = (j.log || []).join('\n');
  if (j.status === 'done' || j.status === 'error') {
    clearInterval(state.polling); state.polling = null;
    $('#jobcard').className = 'panel notice ' + (j.status === 'done' ? 'ok' : 'danger');
    $('#jobstatus').textContent = j.status === 'done' ? t("job.completed", {v0: j.finished}) : t("job.failed", {v0: j.error || t("job.unknown_error")});
    $('#jobactions').innerHTML = `<button id="jobClose">${t("job.close")}</button>`;
    clearInterval(state.autoClose); state.autoClose = null;
    const closeCard = () => { clearInterval(state.autoClose); state.autoClose = null; $('#jobcard').classList.add('hide'); };
    $('#jobClose').onclick = closeCard;
    if (j.status === 'done') {   // 成功的 15 秒後自動收起；失敗的留著，要讓人看到
      // 只在看得到的時候倒數：視窗在背景或滑鼠停在卡片上就暫停，更新跑完時人不在座位也不會錯過結果
      let n = 15; $('#jobClose').textContent = t("job.close_auto", {n});
      const card = $('#jobcard'); card.onmouseenter = () => { state.autoClosePaused = true; }; card.onmouseleave = () => { state.autoClosePaused = false; };
      state.autoClosePaused = card.matches(':hover');
      state.autoClose = setInterval(() => {
        const b = $('#jobClose'); if (!b) { clearInterval(state.autoClose); return; }
        if (document.visibilityState !== 'visible' || state.autoClosePaused) return;
        n -= 1; if (n <= 0) closeCard(); else b.textContent = t("job.close_auto", {n});
      }, 1000);
    }
    state.apps = null; await load(); loadFirmware(true);
    if (!$('#tab-apps').classList.contains('hide')) loadApps(true);
    if (!$('#tab-disk').classList.contains('hide')) loadDisk(true);
  }
}

