/* 設定分頁：選用功能清單。每項：狀態、給你什麼、要付出什麼、路徑已填好的指令（一鍵複製）、能按的就給按鈕。 */
async function loadSetup() {
  const box = $('#setup'); if (!box) return;
  const j = await api('/api/setup');
  if (!j.ok) { box.innerHTML = `<div class="empty">${esc(j.error)}</div>`; return; }
  const on = j.items.filter(i => i.enabled).length, applicable = j.items.filter(i => i.applies !== false);
  $('#setupMeta').textContent = t("setup.meta", {on, n: applicable.length, when: j.generated.replace('T', ' ')});
  $('#dotSetup').classList.toggle('hide', !j.items.some(i => i.recommended && !i.enabled));   // 只有「建議裝但沒裝」才亮點
  let html = '';
  for (const it of j.items) {
    if (it.applies === false) continue;   // 這台用不到的（例如沒裝 DGX Dashboard、沒裝 Chrome）就不列，免得新手以為要處理
    const st = it.enabled ? `<span class="tag ok">${t("setup.enabled")}</span>` : it.recommended ? `<span class="tag sec">${t("setup.recommended")}</span>` : `<span class="tag">${t("setup.disabled")}</span>`;
    const cmds = it.commands.length && !it.enabled ? `<div style="margin-top:8px;display:flex;gap:8px;align-items:flex-start"><pre class="cl" style="flex:1">${esc(it.commands.join('\n'))}</pre><button class="small" data-copy="${esc(it.commands.join('\n'))}">${t("setup.copy")}</button></div>` : '';
    let action = '';
    if (it.action === 'pwa_install' && !it.enabled && !j.readonly) action = `<div style="margin-top:8px"><button class="small primary" data-act="pwa_install">${t("setup.pwa_button")}</button> <span class="sub1">${t("setup.pwa_button_hint")}</span></div>`;
    if (it.action === 'autostart' && !j.readonly) action = `<div style="margin-top:8px"><label style="cursor:pointer"><input type="checkbox" data-act="autostart" ${it.enabled ? 'checked' : ''}> ${t("common.open_at_login")}</label></div>`;
    const detail = it.id === 'node_helper' && it.detail ? `<div class="sub1">${t("setup.node_detail", {node: esc(it.detail.node || '—'), lts: esc(it.detail.lts || '—')})}</div>` : '';
    html += `<div class="panel"><div class="panel-h"><h2>${t("setup." + it.id + ".title")}</h2>${st}</div>
      <div style="margin-top:6px">${t("setup." + it.id + ".gives")}</div>
      <div class="sub1" style="margin-top:4px">${t("setup." + it.id + ".costs")}</div>${detail}${cmds}${action}</div>`;
  }
  html += `<div class="sub1" style="margin:4px 0 20px">${t("setup.footer")}</div>`;
  box.innerHTML = html;
  box.querySelectorAll('button[data-copy]').forEach(b => b.onclick = async () => { try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent = t("setup.copied"); setTimeout(() => b.textContent = t("setup.copy"), 1500); } catch (e) { alert(t("setup.copy_failed")); } });
  box.querySelectorAll('button[data-act="pwa_install"]').forEach(b => b.onclick = async () => { b.disabled = true; const r = await api('/api/setup/pwa-install', {}); if (!r.ok) { alert(r.error); b.disabled = false; return; } b.textContent = t("setup.pwa_opened"); });
  box.querySelectorAll('input[data-act="autostart"]').forEach(cb => cb.onchange = async () => { cb.disabled = true; const r = await api('/api/autostart', {enabled: cb.checked}); cb.disabled = false; if (!r.ok) alert(r.error || t("common.setting_failed")); cb.checked = !!r.enabled; const top = $('#autostart'); if (top) top.checked = cb.checked; });
}
$('#btnSetupReload').onclick = async () => { const b = $('#btnSetupReload'); b.disabled = true; $('#setupMeta').textContent = t("updates.loading"); await loadSetup(); b.disabled = false; };
