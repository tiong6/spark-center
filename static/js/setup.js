/* 設定分頁：每個選用功能一個開關。按下去由服務用 pkexec 跑一個明確的 install／rm（跳一次密碼視窗），不是叫你貼指令。
   指令仍收在「自己動手也可以」裡，給不想用開關、或在 SSH 裡的人。 */
async function loadSetup(data) {
  const box = $('#setup'); if (!box) return;
  const j = data || await api('/api/setup');
  if (!j.ok && !j.items) { box.innerHTML = `<div class="empty">${esc(j.error)}</div>`; return; }
  const on = j.items.filter(i => i.enabled).length, applicable = j.items.filter(i => i.applies !== false);
  $('#setupMeta').textContent = t("setup.meta", {on, n: applicable.length, when: (j.generated || '').replace('T', ' ')});
  $('#dotSetup').classList.toggle('hide', !j.items.some(i => i.recommended && !i.enabled));
  let html = j.readonly ? `<div class="panel notice warn">${t("setup.readonly")}</div>` : '';
  for (const it of j.items) {
    if (it.applies === false) continue;
    const st = it.id === 'usbc' ? (it.state === 'default' ? `<span class="tag ok">${t("setup.usbc_default")}</span>` : it.state === 'calibrated' ? `<span class="tag ok">${t("setup.usbc_calibrated", {n: it.n})}</span>` : `<span class="tag sec">${t("setup.usbc_none")}</span>`)
             : it.enabled ? `<span class="tag ok">${t("setup.enabled")}</span>` : it.recommended ? `<span class="tag sec">${t("setup.recommended")}</span>` : '';
    let control;
    if (it.id === 'usbc') control = `<a class="btnlink small" href="#hardware" data-act="goto-hardware">${t("setup.usbc_go")}</a>`;
    else if (it.id === 'pwa') control = it.enabled ? '' : `<button class="small primary" data-act="pwa_install" ${j.readonly ? 'disabled' : ''}>${t("setup.pwa_button")}</button>`;
    else control = `<label class="switchrow"><input type="checkbox" data-toggle="${it.id}" ${it.enabled ? 'checked' : ''} ${j.readonly ? 'disabled' : ''}><span>${it.enabled ? t("setup.on") : t("setup.off")}</span></label>`;
    const rd = it.id === 'remote' && it.detail ? it.detail : null;
    const remote = rd ? `<div style="margin-top:8px"><b>${t("setup.remote_sync_title")}</b><table style="margin-top:4px;width:auto"><tbody>
        <tr><td class="sub1" style="padding:2px 12px 2px 0">Name</td><td class="mono">Spark Center</td></tr>
        <tr><td class="sub1" style="padding:2px 12px 2px 0">Port</td><td class="mono">${rd.port}</td></tr>
        <tr><td class="sub1" style="padding:2px 12px 2px 0">Auto open in browser</td><td>${t("setup.remote_tick")}</td></tr>
        <tr><td class="sub1" style="padding:2px 12px 2px 0">URL Path / Launch Script</td><td class="sub1">${t("setup.remote_blank")}</td></tr></tbody></table>
        <div class="sub1" style="margin-top:6px">${t("setup.remote_addr", {mdns: esc(rd.mdns || '—'), ips: esc(rd.ips.join(', ') || '—'), ts: rd.tailscale ? ` · Tailscale ${esc(rd.tailscale)}` : '', user: esc(rd.user), port: rd.port})}</div>
        ${it.enabled && !rd.boot ? `<div class="sub1">${t("setup.remote_not_boot")}</div>` : ''}</div>` : '';
    const detail = remote + (it.id === 'node_helper' && it.detail ? `<div class="sub1">${t("setup.node_detail", {node: esc(it.detail.node || '—'), lts: esc(it.detail.lts || '—')})}</div>` : '');
    const prompt = it.sudo ? `<div class="sub1">${t(it.id === 'node_helper' ? "setup.prompt_two" : it.id === 'remote' ? "setup.prompt_remote" : "setup.prompt_one")}</div>` : '';
    const cmds = it.commands.length ? `<details style="margin-top:8px"><summary class="sub1" style="cursor:pointer">${t("setup.diy")}</summary><div style="margin-top:6px;display:flex;gap:8px;align-items:flex-start"><pre class="cl" style="flex:1">${esc(it.commands.join('\n'))}</pre><button class="small" data-copy="${esc(it.commands.join('\n'))}">${t("setup.copy")}</button></div></details>` : '';
    html += `<div class="panel"><div class="panel-h"><h2>${t("setup." + it.id + ".title")}</h2>${st}<div class="actions">${control}</div></div>
      <div style="margin-top:6px">${t("setup." + it.id + ".gives")}</div>
      <div class="sub1" style="margin-top:4px">${t("setup." + it.id + ".costs")}</div>${detail}${prompt}${it.id === 'pwa' && !it.enabled ? `<div class="sub1">${t("setup.pwa_button_hint")}</div>` : ''}${cmds}</div>`;
  }
  html += `<div class="sub1" style="margin:4px 0 20px">${t("setup.footer")}</div>`;
  box.innerHTML = html;
  box.querySelectorAll('button[data-copy]').forEach(b => b.onclick = async () => { try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent = t("setup.copied"); setTimeout(() => b.textContent = t("setup.copy"), 1500); } catch (e) { alert(t("setup.copy_failed")); } });
  box.querySelectorAll('a[data-act="goto-hardware"]').forEach(a => a.onclick = e => { e.preventDefault(); showTab('hardware'); });
  box.querySelectorAll('button[data-act="pwa_install"]').forEach(b => b.onclick = async () => { b.disabled = true; const r = await api('/api/setup/pwa-install', {}); if (!r.ok) { alert(r.error); b.disabled = false; return; } b.textContent = t("setup.pwa_opened"); });
  box.querySelectorAll('input[data-toggle]').forEach(cb => cb.onchange = async () => {
    const want = cb.checked; cb.disabled = true; const lab = cb.nextElementSibling; lab.textContent = t("setup.waiting");
    const r = await api('/api/setup/toggle', {item: cb.dataset.toggle, enable: want});
    if (!r.ok) { alert(r.error); const d = cb.closest('.panel') && cb.closest('.panel').querySelector('details'); if (d) d.open = true; }   // 失敗時把「自己動手」攤開
    if (r.items) loadSetup(r); else loadSetup();   // 以伺服器回報的實際狀態重畫，不信任本地勾選
    if (cb.dataset.toggle === 'autostart') { const top = $('#autostart'); if (top && r.items) top.checked = !!r.items.find(i => i.id === 'autostart').enabled; }
  });
}
$('#btnSetupReload').onclick = async () => { const b = $('#btnSetupReload'); b.disabled = true; $('#setupMeta').textContent = t("updates.loading"); await loadSetup(); b.disabled = false; };
