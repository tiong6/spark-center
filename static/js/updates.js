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
  const active = state.items.filter(it => !(it.firmware && it.firmware.passive) && !it.node_flow);
  $('#dotUpd').classList.toggle('hide', active.length === 0);
  if (!state.items.length) { box.innerHTML = `<div class="empty">${t("updates.there_are_currently_no_upgradable_packages")}</div>`; updateGo(); return; }
  const THEAD = `<thead><tr><th style="width:36px"></th><th style="width:32%">${t("updates.package")}</th><th style="width:24%">${t("updates.version")}</th><th class="col-opt">${t("updates.description")}</th><th style="width:80px">${t("updates.size")}</th><th style="width:80px"></th></tr></thead>`;
  const row = it => `<tr class="sub">
        <td><input type="checkbox" data-pkg="${esc(it.name)}" ${it.node_flow ? 'disabled' : (state.selected.has(it.name)?'checked':'')}></td>
        <td>${esc(it.name)}${state.newAfterRefresh && state.newAfterRefresh.has(it.name) ? `<span class="tag ok" title="${t("updates.new_after_refresh_hint")}">${t("updates.new_after_refresh")}</span>` : ''}${it.node_flow?`<span class="tag" title="${t("updates.node_flow_hint")}">${t("updates.node_flow")}</span>`:''}${it.security?'<span class="tag sec">security</span>':''}${it.spark_core?`<span class="tag" style="color:#76b900;border-color:#4b6b1e" title="${t("updates.spark_os_nvidia_platform_packages_dashboard")}">${t("updates.spark_os_use_the_stock_dashboard")}</span>`:''}${it.reboot_hint?`<span class="tag reboot" title="${t("updates.based_on_the_package_name_estimated")}">${t("updates.usually_needs_reboot_estimated")}</span>`:''}${it.firmware && it.firmware.passive?`<span class="tag" title="${t("updates.none_of_the_loaded_kernel_modules", {v0: esc(it.firmware.pulled_by ? t("updates.pulled_in_as_a_dependency_of", {v0: it.firmware.pulled_by}) : t("updates.automatically_installed")), v1: it.firmware.files})}">${t("updates.no_local_driver_declares_use")}</span>`:''}</td>
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
  box.querySelectorAll('input[data-group]').forEach(cb => cb.onchange = () => { for (const it of state.items) if (it.group === cb.dataset.group && !passive.includes(it) && !it.node_flow) cb.checked ? state.selected.add(it.name) : state.selected.delete(it.name); renderList(); });
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
  $('#fwMeta').textContent = (j.bios ? t("updates.fw_bios", {v: j.bios.version, d: j.bios.date}) + ' · ' : '') + t("updates.fwupd_updatable_devices_updates_available_version", {v0: j.fwupd_version || '—', v1: j.devices.length, v2: q(j.updates), v3: q(j.mismatches), v4: q(j.pending), v5: j.generated.replace('T',' ')});
  $('#btnFwUpdate').classList.toggle('hide', !j.updates);
  $('#btnFwReboot').classList.toggle('hide', !j.pending);   // capsule 已排入 EFI 分割區，要重開才燒錄；按了才重開，不自動
  const errBanner = j.error ? `<div class="panel notice danger" style="margin-bottom:10px">${esc(j.error)}</div>` : '';
  const vis = j.devices.filter(d => !d.hidden), hid = j.devices.filter(d => d.hidden);
  // 分組＋白話：fwupd 的名字（UEFI Device Firmware ×2、KEK CA、SBAT…）一般人看不懂。
  // 群組看 plugin；裝置名優先用 LVFS 的 release 名（GX10 SoC FW／USB-C PD FW），說明用固定表。
  const SECURE = ['uefi_db', 'uefi_kek', 'uefi_dbx', 'uefi_sbat', 'uefi_pk'];
  const groupOf = d => d.plugin === 'uefi_capsule' ? 'board' : SECURE.includes(d.plugin) ? 'secure' : d.plugin === 'nvme' ? 'storage' : 'peripheral';
  const kindOf = d => { const n = [(d.notes || {}).lvfs_name, d.name, d.summary].filter(Boolean).join(' '); const p = d.plugin || '';
    if (/SoC/i.test(n)) return 'soc'; if (/Embedded Controller/i.test(n)) return 'ec'; if (/USB-C PD|PD FW/i.test(n)) return 'pd';
    if (p === 'uefi_capsule') return 'capsule'; if (p === 'nvme') return 'nvme'; if (p === 'uefi_dbx') return 'dbx'; if (p === 'uefi_sbat') return 'sbat';
    if (SECURE.includes(p)) return 'cert'; if (/usb4|hub/i.test(p + n)) return 'hub'; if (/hidpp|mouse|keyboard|receiver/i.test(p + n)) return 'hid'; return 'generic'; };
  const friendly = d => { const k = kindOf(d); return ['generic', 'cert', 'hid', 'capsule', 'dbx', 'sbat'].includes(k) ? d.name : t("updates.fwd_" + k + "_name"); };
  const row = d => { const last = d.history[0]; const st = d.mismatch ? `<span class="tag reboot">${t("updates.history_reports_success_but_version_differs")}</span>` : d.pending ? `<span class="tag sec">${t("updates.awaiting_reboot_to_apply")}</span>` : d.update_available ? `<span class="tag sec">${t("updates.new_version", {v0: esc(d.latest)})}</span>` : j.updates == null ? `<span class="tag">${t("updates.unknown_query_failed")}</span>` : `<span class="tag ok">${t("updates.up_to_date")}</span>`;
    // LVFS 的版本說明另起一列橫跨整張表：塞在 30% 寬的裝置欄裡會把一列撐到兩百多像素高、右邊三欄空著
    // ASUS 的說明只寫「check NV's release note」卻沒連結：抓 OTA 代號（26=年、07=月，對過官方頁面 2604=四月、2607=七月）補上 NVIDIA 釋出說明連結，標「依編號推定」
    const otaLink = txt => { const m = /OTA\s?(\d{2})(\d{2})(\.\d+)?/.exec(txt || ''); if (!m) return '';
      const y = 2000 + +m[1], mo = +m[2]; if (mo < 1 || mo > 12) return '';
      const when = LANG === 'zh-TW' ? `${y} 年 ${mo} 月` : `${new Date(y, mo - 1, 1).toLocaleString('en', {month: 'long'})} ${y}`;
      return `<div style="margin-top:4px">${t("updates.fw_notes_ota", {code: esc(m[0]), when})} <a href="https://docs.nvidia.com/dgx/dgx-spark/release-notes.html" target="_blank" rel="noopener">${t("updates.fw_notes_ota_link")}</a></div>`; };
    const n = d.notes || {}, sec = (k, label) => n[k] && (n[k].summary || n[k].text) ? `<div style="margin-top:6px"><b>${label}</b> <span style="opacity:.7">· ${t("updates.fw_notes_released", {d: esc(n[k].date || '—')})}</span><div style="margin:2px 0 0 12px;white-space:pre-line">${esc(n[k].summary || '')}${n[k].text ? '\n' + esc(n[k].text) : ''}${otaLink(n[k].text)}</div></div>` : '';
    // 有新版時使用者要看的是「新版改了什麼」：新版排前、預設展開
    const notes = sec('latest', t("updates.fw_notes_latest", {v: esc((n.latest || {}).version || '')})) + sec('installed', t("updates.fw_notes_installed", {v: esc((n.installed || {}).version || '')}));
    const noteRow = notes ? `<tr class="fwnote"><td colspan="4" style="padding:0 12px 10px 28px;border-top:0"><details class="sub1"${d.update_available && n.latest ? ' open' : ''}><summary style="cursor:pointer">${t("updates.fw_notes_toggle")}</summary>${notes}</details></td></tr>` : '';
    const raw = friendly(d) !== d.name ? `${esc(d.name)} · ` : '';
    return `<tr><td><b>${esc(friendly(d))}</b><div class="sub1">${t("updates.fwd_" + kindOf(d))}</div><div class="sub1" style="opacity:.7">${raw}${esc(d.vendor || '')}</div></td><td class="mono">${esc(d.version || '—')}</td><td>${st}</td><td class="sub1">${last ? `${esc(last.old || '?')} → ${esc(last.new || '?')}，${esc(last.state_zh)}${last.error ? '：' + esc(last.error) : ''}<br>${esc((last.when || '').replace('T',' '))}` : '—'}</td></tr>${noteRow}`; };
  const order = ['board', 'storage', 'peripheral', 'secure'];
  let html = errBanner;
  for (const g of order) {
    const items = vis.filter(d => groupOf(d) === g); if (!items.length) continue;
    const body = `<table><thead><tr><th style="width:30%">${t("updates.device")}</th><th style="width:16%">${t("updates.current_version_2")}</th><th style="width:20%">${t("updates.status")}</th><th>${t("updates.last_update_fwupd_history")}</th></tr></thead><tbody>${items.map(row).join('')}</tbody></table>`;
    // 開機安全那組預設收起：憑證與撤銷清單有更新裝就好，不需要理解
    html += g === 'secure'
      ? `<details style="margin-top:10px"><summary class="sub1" style="cursor:pointer"><b>${t("updates.fwg_" + g)}</b>（${items.length}）· ${t("updates.fwg_" + g + "_desc")}</summary>${body}</details>`
      : `<div style="margin-top:${html ? 14 : 0}px"><b>${t("updates.fwg_" + g)}</b> <span class="sub1">· ${t("updates.fwg_" + g + "_desc")}</span></div>${body}`;
  }
  if (hid.length) html += `<details style="margin-top:8px"><summary class="sub1">${t("updates.hidden_devices_certificates_and_keys", {v0: hid.length})}</summary><table><tbody>${hid.map(row).join('')}</tbody></table></details>`;
  html += `<div class="sub1" style="margin-top:8px">${t("updates.version_mismatch_means_fwupd_history_records")}</div>`;
  box.innerHTML = html;
}
$('#btnFwRefresh').onclick = async () => { $('#fwMeta').textContent = t("updates.querying_lvfs"); const r = await api('/api/disk/action', {action: 'fwupd_refresh'}); if (!r.ok) { alert(r.error); return; } startPolling(t("updates.fwupd_refresh_lvfs_metadata")); const w = setInterval(async () => { const jj = await api('/api/job'); if (jj.status !== 'running') { clearInterval(w); loadFirmware(true); } }, 1500); };
$('#btnFwUpdate').onclick = async () => { if (!confirm(t("updates.install_all_available_firmware_updates_with"))) return; const r = await api('/api/disk/action', {action: 'fwupd_update'}); if (!r.ok) { alert(r.error); return; } startPolling(t("updates.fwupd_install_firmware_updates")); window.scrollTo({top: 0, behavior: 'smooth'}); };

/* ---- Spark Center 自己的更新：git fetch 比對 origin，有新版列 commit 標題，一顆「更新並重啟」 ---- */
async function loadSelf(force) {
  const panel = $('#selfPanel'), box = $('#self'); if (!panel) return;
  const j = await api('/api/self' + (force ? '?force=1' : ''));
  if (!j.ok) { panel.classList.remove('hide'); box.innerHTML = `<div class="empty">${esc(j.error)}</div>`; return; }
  if (!j.git) { panel.classList.add('hide'); return; }   // 不是 git clone 裝的：這張卡沒有意義
  panel.classList.remove('hide');
  const h = j.head || {}, r = j.remote || {};
  $('#selfMeta').textContent = t("self.meta", {head: h.hash || '—', date: h.date || '—', when: j.checked ? j.checked.replace('T', ' ') : '—'});
  $('#btnSelfUpdate').classList.toggle('hide', !j.can_update);
  $('#selfBadge').classList.toggle('hide', !(j.behind > 0));
  let html = j.fetch_error ? `<div class="panel notice warn" style="margin-bottom:10px">${esc(j.fetch_error)}</div>` : '';
  if (!j.behind) html += `<div class="empty">${t("self.up_to_date", {head: esc(h.hash || '—'), subject: esc(h.subject || '')})}</div>`;
  else {
    html += `<p>${t("self.behind", {n: j.behind, hash: esc(r.hash || '—'), date: esc(r.date || '—')})}</p>`;
    if (j.dirty.length) html += `<div class="panel notice danger">${t("self.dirty", {files: esc(j.dirty.join(', '))})}</div>`;
    if (j.ahead) html += `<div class="panel notice warn">${t("self.ahead", {n: j.ahead})}</div>`;
    if (j.helper_changed) html += `<div class="panel notice warn">${t("self.helper_changed")}</div>`;
    html += `<div class="chg">` + j.commits.map(c => `<div>${esc(c.date)} ${esc(c.hash)} ${esc(c.subject)}</div>`).join('') + `</div>`;
    html += `<p class="sub1" style="margin-top:10px">${t("self.how")}</p>`;
  }
  box.innerHTML = html;
}
$('#btnSelfCheck').onclick = async () => { const b = $('#btnSelfCheck'); b.disabled = true; $('#selfMeta').textContent = t("self.checking"); await loadSelf(true); b.disabled = false; };
$('#btnSelfUpdate').onclick = async () => {
  if (!confirm(t("self.confirm"))) return;
  const b = $('#btnSelfUpdate'); b.disabled = true;
  const r = await api('/api/self/update', {});
  if (!r.ok) { alert(r.error); b.disabled = false; return; }
  $('#self').innerHTML = `<div class="panel notice ok">${t("self.updated", {head: esc(r.head || '')})}${r.helper_changed ? `<div style="margin-top:6px">${t("self.helper_changed")}</div>` : ''}<div class="sub1" style="margin-top:6px">${t("self.restarting")}</div></div>`;
  // 服務會在 1 秒後重啟：等它回來再重新載入頁面
  const t0 = Date.now(); await new Promise(res => setTimeout(res, 2500));
  const wait = setInterval(async () => { try { const m = await api('/api/machine'); if (m.ok) { clearInterval(wait); location.reload(); } } catch (e) {} if (Date.now() - t0 > 60000) clearInterval(wait); }, 1500);
};

/* ---- npm 全域套件：Claude Code、Gemini CLI、OpenClaw 這類 CLI 工具，apt/snap/flatpak 都看不到 ---- */
async function loadNpm(force) {
  const box = $('#npm'); if (!box) return;
  const j = await api('/api/npm' + (force ? '?force=1' : ''));
  if (!j.ok || !j.available) { box.innerHTML = `<div class="empty">${esc(j.error || j.note || '—')}</div>`; $('#npmMeta').textContent = ''; $('#btnNpmUpdate').classList.add('hide'); return; }
  const q = v => v == null ? '—' : v;   // 查不到新版時顯示「—」，不顯示 0
  const ni = j.node_info || {};
  // 不寫死版本號：現行 LTS 與支援期限來自 Node 官方版本表（查不到就「—」），套件要求的版本來自各自的 engines
  $('#npmMeta').textContent = t("updates.npm_meta", {node: j.node || '—', end: ni.installed_end || '—', lts: ni.lts_version || '—', npm: j.npm || '—', n: j.packages.length, o: q(j.outdated), prefix: j.prefix || '—', when: j.generated.replace('T', ' ')});
  $('#btnNpmUpdate').classList.toggle('hide', !j.outdated);
  let html = j.error ? `<div class="panel notice danger" style="margin-bottom:10px">${esc(j.error)}</div>` : '';
  if (ni.error) html += `<div class="sub1" style="margin-top:10px">${esc(ni.error)}</div>`;
  // 有套件的可裝版被 Node 版本卡住（registry 最新版比 npm 認定可裝的新，或已裝版比可裝版新）→ 根源是 Node 太舊，提醒放最上面
  const held = j.packages.filter(p => p.newer || p.blocked || (p.dist_latest && p.latest && p.dist_latest !== p.latest));
  const reqMajor = Math.max(0, ...held.map(p => parseInt(((p.blocked ? p.target_engines : p.engines_node) || '').replace(/[^0-9.]/g, ' ').trim().split(/[\s.]/)[0]) || 0));
  const instMajor = parseInt((j.node || '0').split('.')[0]) || 0;
  // 黃框裡留一個位子給「Node 主版本升級」那塊：動作跟著原因走，放在套件表下面會看不到
  if (held.length) html += `<div class="panel notice warn" style="margin:12px 0 10px">${t("updates.npm_node_old", {node: esc(j.node || '—'), list: held.map(p => esc(p.name) + ((p.blocked ? p.target_engines : p.engines_node) ? ` (Node ${esc(p.blocked ? p.target_engines : p.engines_node)})` : '')).join('、'), req: reqMajor || '—', lts: ni.lts_major || '—'})}<div id="nodeSlot" style="margin-top:10px"></div></div>`;
  else if (ni.lts_major && instMajor && instMajor < ni.lts_major) html += `<div class="sub1" style="margin:10px 0 6px">${t("updates.npm_node_older_lts", {major: instMajor, end: ni.installed_end || '—', lts: ni.lts_major})}</div>`;
  if (!j.packages.length) { box.innerHTML = html + `<div class="empty">${t("updates.npm_none")}</div>`; return; }
  html += `<table><thead><tr><th style="width:34%">${t("updates.package")}</th><th style="width:16%">${t("updates.current_version_2")}</th><th style="width:16%" title="${t("updates.npm_latest_hint")}">${t("updates.npm_latest")}</th><th>${t("updates.status")}</th><th style="width:120px"></th></tr></thead><tbody>`;
  for (const p of j.packages) {
    const st = p.outdated ? `<span class="tag sec">${t("updates.new_version", {v0: esc(p.latest)})}</span>` : p.blocked ? `<span class="tag" title="${esc(p.blocked_reason || '')}">${t("updates.npm_blocked", {v0: esc(p.latest)})}</span><span class="sub1"> ${esc(p.blocked_reason || '')}</span>` : p.newer ? `<span class="tag" title="${t("updates.npm_newer_hint")}">${t("updates.npm_newer", {v0: esc(p.latest)})}</span>` : j.outdated == null ? `<span class="tag">${t("updates.unknown_query_failed")}</span>` : `<span class="tag ok">${t("updates.up_to_date")}</span>`;
    // 名稱下面放描述、作者、倉庫：都是套件自己宣稱的，npm 不驗證，所以只照實顯示、附連結讓人自己看
    const repoText = p.repo ? p.repo.replace(/^https?:\/\//, '') : null;
    const who = [p.author ? esc(p.author) : null, repoText ? `<a href="${esc(p.repo)}" target="_blank" rel="noopener">${esc(repoText)}</a>` : (p.homepage ? `<a href="${esc(p.homepage)}" target="_blank" rel="noopener">${esc(p.homepage.replace(/^https?:\/\//, ''))}</a>` : null), p.license ? esc(p.license) : null].filter(Boolean).join(' · ');
    const stale = (p.running || []).filter(r => r.stale);
    const runTag = (p.running || []).length ? `<span class="tag${stale.length ? ' reboot' : ''}" title="${esc((p.running || []).map(r => `PID ${r.pid}${r.unit ? ' · ' + r.unit : ''}`).join('\n'))}">${stale.length ? t("updates.npm_running_stale", {n: stale.length}) : t("updates.npm_running", {n: p.running.length})}</span>` : '';
    // 跑的是更新前的舊版（程序比磁碟上的 package.json 舊）：面板常駐一顆重啟鈕，不靠工作卡片
    const staleUnits = [...new Set(stale.map(r => r.unit).filter(Boolean))];
    const staleBtns = stale.length ? `<div class="sub1" style="margin-top:4px">${t("updates.npm_stale_hint")} ${staleUnits.map(u => `<button class="small" data-restart="${esc(u)}">${t("updates.npm_restart_btn", {unit: esc(u)})}</button>`).join(' ')}${stale.some(r => !r.unit) ? ` ${esc(t("updates.npm_restart_manual"))}` : ''}</div>` : '';
    html += `<tr><td><span class="mono">${esc(p.name)}</span>${runTag}${p.description ? `<div class="sub1">${esc(p.description)}</div>` : ''}<div class="sub1">${who || t("updates.npm_no_meta")}</div>${staleBtns}</td><td class="mono">${esc(p.current || '—')}</td><td class="mono">${esc(p.latest || '—')}</td><td>${st}</td><td>${p.outdated ? `<button class="small primary" data-npm="${esc(p.name)}">${t("common.update")}</button>` : p.newer ? `<button class="small" data-npm="${esc(p.name)}" title="${t("updates.npm_newer_hint")}">${t("updates.npm_install_compat", {v0: esc(p.latest)})}</button>` : ''}</td></tr>`;
  }
  box.innerHTML = html + `</tbody></table><div class="sub1" style="margin-top:8px">${t("updates.npm_trust_note")}</div>`;
  state.npmPkgs = j.packages;
  const slot = box.querySelector('#nodeSlot'), ns = $('#nodeSource');
  if (slot && ns) slot.appendChild(ns);   // 搬 DOM 節點，loadNodeSource 之後不管先後都會渲染到它現在的位置
  box.querySelectorAll('button[data-npm]').forEach(b => b.onclick = () => npmUpdate([b.dataset.npm], b));
  box.querySelectorAll('button[data-restart]').forEach(b => b.onclick = async () => {
    b.disabled = true; b.textContent = t("updates.npm_restarting");
    const r = await api('/api/npm/restart', {unit: b.dataset.restart});
    if (!r.ok) { b.textContent = r.error; b.disabled = false; return; }
    loadNpm(true);
  });
  $('#btnNpmUpdate').onclick = () => npmUpdate(j.packages.filter(p => p.outdated).map(p => p.name), $('#btnNpmUpdate'));
}
/* 更新前先講清楚會發生什麼：停哪些服務、認不認識這個程式、要不要備份、會不會升資料格式。同一個視窗也給降回用（rollback=true）。 */
function npmPlanHtml(plan, rollback) {
  let h = '';
  for (const it of plan.items) {
    h += `<div style="margin-top:10px"><b class="mono">${esc(it.name)}</b> <span class="mono sub1">${esc(it.current || '—')} → ${esc(it.target || '—')}</span>`;
    if (it.units.length) h += `<div class="sub1">${t("updates.npm_plan_units", {units: esc(it.units.join('、'))})}</div>`;
    if (it.loose_pids.length) h += `<div class="sub1">${t("updates.npm_plan_loose", {pids: esc(it.loose_pids.join(', '))})}</div>`;
    if (it.known) {
      h += `<div class="sub1">${t(rollback ? "updates.npm_plan_known_rollback" : "updates.npm_plan_known", {name: esc(it.name)})}</div>`;
      if (it.migrates_data) h += `<div class="panel notice ${rollback ? 'danger' : 'warn'}" style="margin-top:6px">${t(rollback ? "updates.npm_plan_migrates_rollback" : "updates.npm_plan_migrates", {name: esc(it.name)})}</div>`;
      if (it.can_backup) h += `<label style="display:block;margin-top:6px;cursor:pointer"><input type="checkbox" id="npmBackup" checked> ${t("updates.npm_plan_backup", {dir: esc(it.data_dir || '')})}</label>`;
    } else if (it.units.length) h += `<div class="sub1">${t("updates.npm_plan_unknown")}</div>`;
    h += `</div>`;
  }
  h += `<p class="sub1" style="margin-top:12px">${t("updates.npm_plan_steps")}</p>`;
  return h;
}
async function npmUpdate(names, btn) {
  btn.disabled = true;
  const plan = await api('/api/npm/plan', {names});
  btn.disabled = false;
  if (!plan.ok) { alert(plan.error); return; }
  $('#mBody').innerHTML = `<p>${t("updates.npm_plan_title", {list: esc(names.join(', '))})}</p>` + npmPlanHtml(plan, false);
  $('#mOk').classList.remove('hide'); $('#mOk').textContent = t("common.confirm_update");
  $('#mOk').onclick = async () => {
    const backup = !$('#npmBackup') || $('#npmBackup').checked; closeModal();
    const r = await api('/api/npm/update', {names, backup});
    if (!r.ok) { alert(t("updates.could_not_start_with_value", {value: r.error})); return; }
    startPolling(t("job.npm_update_with_value", {value: names.join(', ')})); window.scrollTo({top: 0, behavior: 'smooth'});
  };
  openModal(t("updates.confirm_changes"));
}
/* 服務在 npm 更新後起不來：先用人話講原因，再給可以按的修法；原始日誌在下面本來就看得到 */
function renderNpmFailure(j) {
  const box = document.createElement('div'); box.style.marginTop = '10px';
  let h = `<div class="panel notice danger"><b>${t("updates.npm_failure_what")}</b><div style="margin-top:6px">${t("updates." + j.reason, {name: esc(j.reason_name || '')})}</div></div>`;
  h += `<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:8px;align-items:center">`;
  for (const a of j.actions || []) {
    if (a.id === 'forward') h += `<button class="small primary" data-npmact="forward" data-name="${esc(a.name)}" data-version="${esc(a.version)}" title="${t("updates.npm_act_forward_hint")}">${t("updates.npm_act_forward", {v: esc(a.version)})}</button>`;
    if (a.id === 'repair') h += `<button class="small primary" data-npmact="repair" data-name="${esc(a.name)}" title="${t("updates.npm_act_repair_hint")}">${t("updates.npm_act_repair", {name: esc(a.name)})}</button>`;
    if (a.id === 'restart') for (const u of a.units) h += `<button class="small" data-restart="${esc(u)}">${t("updates.npm_restart_btn", {unit: esc(u)})}</button>`;
    if (a.id === 'backup_info') h += `<span class="sub1">${t("updates.npm_act_backup", {path: esc(a.path)})}${a.docs ? ` · <a href="${esc(a.docs)}" target="_blank" rel="noopener">${t("updates.npm_act_docs")}</a>` : ''}</span>`;
  }
  h += `</div>`;
  box.innerHTML = h; $('#jobactions').appendChild(box);
  box.querySelectorAll('button[data-npmact]').forEach(b => b.onclick = async () => {
    b.disabled = true;
    const r = await api('/api/npm/action', {action: b.dataset.npmact, name: b.dataset.name, version: b.dataset.version});
    if (!r.ok) { alert(r.error); b.disabled = false; return; }
    startPolling(t(b.dataset.npmact === 'forward' ? "job.npm_forward_with_value" : "job.npm_repair_with_value", {value: b.dataset.name}));
  });
  box.querySelectorAll('button[data-restart]').forEach(b => b.onclick = async () => {
    b.disabled = true; b.textContent = t("updates.npm_restarting");
    const r = await api('/api/npm/restart', {unit: b.dataset.restart});
    b.textContent = r.ok ? t("updates.npm_restarted", {unit: b.dataset.restart, state: r.active}) : t("updates.npm_restart_failed_short");
    if (!r.ok) { b.disabled = false; alert(r.error); }
    loadNpm(true);
  });
}

/* npm 更新完：還在跑舊版檔案的程序要重啟。systemd 使用者服務給一鍵重啟（不需密碼）；不是服務的只提醒 */
/* fwupd 更新完：capsule 寫進 EFI 分割區、下次開機由 UEFI 燒錄。卡片要講清楚「還沒生效」，並給一個按了才重開的按鈕。 */
async function offerFwReboot() {
  const j = await api('/api/firmware?force=1'); loadFirmware(true);
  const box = document.createElement('div'); box.className = 'sub1'; box.style.marginTop = '8px';
  if (!j.ok || j.pending == null) { box.textContent = t("updates.fw_staged_unknown"); $('#jobactions').appendChild(box); return; }
  if (!j.pending) { box.textContent = t("updates.fw_staged_none"); $('#jobactions').appendChild(box); return; }
  const names = j.devices.filter(d => d.pending).map(d => `${esc(d.name)} → ${esc((d.history[0] || {}).new || '?')}`).join(LANG === 'zh-TW' ? '、' : ', ');
  box.innerHTML = `<b>${t("updates.fw_staged", {n: j.pending})}</b><div style="margin-top:4px">${names}</div><div style="margin-top:4px">${t("updates.fw_staged_power")}</div><button class="small primary" id="jobReboot" style="margin-top:8px">${t("updates.reboot_now")}</button>`;
  $('#jobactions').appendChild(box); $('#jobReboot').onclick = () => rebootNow($('#jobReboot'));
}
async function rebootNow(btn) {
  if (!confirm(t("updates.reboot_confirm"))) return;
  btn.disabled = true; btn.textContent = t("updates.rebooting");
  const r = await api('/api/reboot', {});
  if (!r.ok) { btn.disabled = false; btn.textContent = t("updates.reboot_now"); alert(r.error); }
}
$('#btnFwReboot').onclick = () => rebootNow($('#btnFwReboot'));
async function offerNpmRestart(names) {
  const j = await api('/api/npm?force=1');
  if (!j.ok) return;
  const hits = (j.packages || []).filter(p => names.includes(p.name) && (p.running || []).length);
  if (!hits.length) return;
  const box = document.createElement('div'); box.className = 'sub1'; box.style.marginTop = '8px';
  let h = `<b>${t("updates.npm_restart_needed")}</b>`;
  for (const p of hits) for (const r of p.running) {
    h += `<div style="margin-top:4px">${esc(p.name)} · PID ${r.pid} · ${r.unit ? `<button class="small" data-restart="${esc(r.unit)}">${t("updates.npm_restart_btn", {unit: esc(r.unit)})}</button>` : esc(t("updates.npm_restart_manual"))}</div>`;
  }
  box.innerHTML = h; $('#jobactions').appendChild(box);
  box.querySelectorAll('button[data-restart]').forEach(b => b.onclick = async () => {
    b.disabled = true; b.textContent = t("updates.npm_restarting");
    const r = await api('/api/npm/restart', {unit: b.dataset.restart});
    b.textContent = r.ok ? t("updates.npm_restarted", {unit: b.dataset.restart, state: r.active}) : r.error;
    if (!r.ok) b.disabled = false;
    loadNpm(true);
  });
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
    let html;
    if (sim.npm) {   // npm 降回沒有密碼視窗、沒有 apt 模擬、沒有設定檔：只講 npm 的計畫
      html = `<p>${t("updates.npm_rollback_confirm", {name: esc(b.dataset.rbName), new: esc(b.dataset.rbNew), old: esc(b.dataset.rbOld)})}</p>`;
      const plan = await api('/api/npm/plan', {names: sim.changes.map(c => c.name), rollback: true, targets: Object.fromEntries(sim.changes.map(c => [c.name, c.to]))});
      if (plan.ok) html += npmPlanHtml(plan, true);
    } else {
      html = `<p>${all ? t("updates.rollback_all_confirm", {list: esc(b.dataset.rbAll)}) : t("updates.rollback_confirm", {name: esc(b.dataset.rbName), new: esc(b.dataset.rbNew), old: esc(b.dataset.rbOld)})}</p>`;
      html += `<p>${t("updates.rollback_sim_summary", {n: sim.changes.length})}</p>`;
      if (extra.length) html += `<div class="panel notice warn">${t("updates.packages_you_did_not_select_will", {v0: extra.length})}</div>`;
      html += `<div class="chg">` + sim.changes.map(c => `<div class="${c.requested?'':'extra'}">${c.action.padEnd(9)} ${esc(c.name)} ${esc(c.from)}${c.to?' → '+esc(c.to):''}</div>`).join('') + `</div>`;
      html += `<p class="muted" style="margin-top:12px">${t("updates.rollback_conffile_note")}</p>`;
    }
    body.innerHTML = html; $('#mOk').classList.remove('hide'); $('#mOk').textContent = t("common.confirm_rollback");
    $('#mOk').onclick = async () => {
      closeModal();
      const backup = !$('#npmBackup') || $('#npmBackup').checked;
      const r = await api('/api/rollback', {job: b.dataset.rbJob, name: b.dataset.rbName, backup});
      if (!r.ok) { alert(r.error); return; }
      startPolling(t("job.rollback_with_value", {value: label})); window.scrollTo({top: 0, behavior: 'smooth'});
    };
    openModal(t("updates.confirm_changes"));
  });
}
async function load() {
  loadFirmware(); loadNpm(); loadRollback(); loadNodeSource(); loadSelf();
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
    state.selected.clear(); startPolling(names.length > 6 ? t("updates.updating_n", {n: names.length}) : t("updates.updating_with_value", {value: names.join(', ')}));
  };
  openModal(t("updates.confirm_changes"));
};
$('#btnRefresh').onclick = async () => { state.namesBeforeRefresh = new Set(state.items.map(i => i.name)); const r = await api('/api/refresh', {}); if (!r.ok) { alert(t("updates.could_not_start_with_value", {value: r.error})); return; } startPolling(t("job.refresh_apt_sources")); };
// 全選只選主清單；折疊區的 firmware 子套件要升級就展開用它自己的群組勾選框
$('#btnAll').onclick = () => { state.items.filter(i => !(i.firmware && i.firmware.passive) && !i.node_flow).forEach(i => state.selected.add(i.name)); renderList(); };
$('#btnNone').onclick = () => { state.selected.clear(); renderList(); };
function openModal(t) { $('#mTitle').textContent = t; $('#overlay').classList.add('open'); }
function closeModal() { $('#overlay').classList.remove('open'); }
$('#mCancel').onclick = closeModal;

function jobTitle(j) {   // 重新整理頁面或服務重啟後接回工作時，標題要對得上實際在做的事
  const pk = (j.packages || []).join(', ');
  const many = (j.packages || []).length > 6;   // 134 個名字塞標題會變一面牆；名單放卡片裡的折疊區
  return ({ node_source: t('node.title'), refresh: t("job.refresh_apt_sources"), install: many ? t("updates.updating_n", {n: j.packages.length}) : t("job.apt_install_with_value", {value: pk}), remove: t("job.apt_remove_with_value", {value: pk}),
            aptclean: t("job.clear_apt_cache"), snap: t("job.snap_update_with_value", {value: pk}), flatpak: t("job.flatpak_update_with_value", {value: pk}),
            ollama_pull: 'ollama pull ' + pk, shell: t("job.system_action_with_value", {value: pk}),
            npm: t("job.npm_update_with_value", {value: pk}), npm_repair: t("job.npm_repair_with_value", {value: pk}), npm_forward: t("job.npm_forward_with_value", {value: (j.packages || [])[0] || pk}),
            rollback: t("job.rollback_with_value", {value: (j.packages || [])[1] || (j.packages || [])[0] || pk}),
            rollback_npm: t("job.rollback_with_value", {value: (j.packages || [])[1] || (j.packages || [])[0] || pk}) })[j.kind] || (t("job.job_with_value", {value: pk}));
}
function startPolling(title) {
  const c = $('#jobcard'); c.className = 'panel notice';
  $('#jobtitle').textContent = title; $('#jobactions').innerHTML = ''; $('#jobpkgs').innerHTML = ''; $('#jobphase').classList.add('hide');
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
  const elapsed = (() => { if (!j.started || j.status !== 'running') return ''; const sec = Math.max(0, Math.round((Date.now() - new Date(j.started)) / 1000));
    return ' · ' + t("job.elapsed", {v: sec >= 60 ? t("job.min_with_value", {value: Math.floor(sec / 60)}) + (sec % 60 ? ' ' + t("job.sec_with_value", {value: sec % 60}) : '') : t("job.sec_with_value", {value: sec})}); })();
  if (j.phase && j.status === 'running') {   // apt 更新：留舊版 → 下載 → 安裝 三段，每段顯示第幾項／共幾項；拿不到步數就不給百分比
    const ORDER = ['prepare', 'wait', 'download', 'install', 'finish'], at = ORDER.indexOf(j.phase);
    $('#jobphase').classList.remove('hide');
    $('#jobphase').innerHTML = ['prepare', 'download', 'install'].map(ph => { const i = ORDER.indexOf(ph); const cls = i < at && !(ph === 'download' && j.phase === 'wait') ? 'done' : i === at ? 'cur' : ''; return `<span class="${cls}">${t("job.phase_" + ph)}</span>`; }).join('');
    const st = j.step ? ` · ${t("job.step_of", {i: j.step[0] + (j.phase === 'prepare' ? 1 : 0), n: j.step[1]})}` : '';
    const cur = j.phase === 'prepare' && j.details ? ` · ${t("job.now_item", {v: j.details})}` : j.phase === 'install' && j.details ? ` · ${j.details}` : j.phase === 'wait' ? ` · ${j.status_text || ''}` : '';
    $('#jobstatus').textContent = t("job.phase_" + j.phase) + st + cur + (j.phase === 'download' ? xferText : '') + elapsed;
    if (j.kind === 'install' && (j.packages || []).length > 6 && !$('#jobpkgs').innerHTML) $('#jobpkgs').innerHTML = `<details><summary style="cursor:pointer">${t("job.package_list", {n: j.packages.length})}</summary><div style="margin-top:4px;line-height:1.7">${j.packages.map(esc).join(', ')}</div></details>`;
  } else {
    $('#jobphase').classList.add('hide');
    $('#jobstatus').textContent = (j.status_text || '') + (j.details ? ' · ' + j.details : '') + (j.status==='running' && j.progress != null ? ` · ${j.progress}%` : '') + xferText + elapsed;
  }
  $('#joblog').textContent = (j.log || []).join('\n');
  if (j.status === 'done' || j.status === 'error') {
    clearInterval(state.polling); state.polling = null;
    $('#jobcard').className = 'panel notice ' + (j.status === 'done' ? 'ok' : 'danger'); $('#jobphase').classList.add('hide');
    $('#jobstatus').textContent = j.status === 'done' ? t("job.completed", {v0: j.finished}) : t("job.failed", {v0: j.error || t("job.unknown_error")});
    $('#jobactions').innerHTML = `<button id="jobClose">${t("job.close")}</button>`;
    if (j.status === 'error' && j.reason) renderNpmFailure(j);   // 人話原因 + 可以按的修法
    clearInterval(state.autoClose); state.autoClose = null;
    const closeCard = () => { clearInterval(state.autoClose); state.autoClose = null; $('#jobcard').classList.add('hide'); };
    $('#jobClose').onclick = closeCard;
    let holdOpen = false;
    if (j.status === 'done' && j.kind === 'shell' && (j.packages || [])[0] === 'fwupd_update') holdOpen = true, offerFwReboot();   // fwupd 的「Successfully installed」只是排入，沒重開不算裝好
    if (j.status === 'done' && j.kind === 'npm') holdOpen = true, offerNpmRestart(j.packages || []);   // 有程序在跑舊版：給重啟鈕，卡片不自動收
    if (j.status === 'done' && !holdOpen) {   // 成功的 15 秒後自動收起；失敗的留著，要讓人看到
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

