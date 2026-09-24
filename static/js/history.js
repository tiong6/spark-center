async function loadHistory() {
  const j = await api('/api/history');
  if (!j.ok) { $('#hist').innerHTML = `<div class="empty">${t("history.could_not_read_history")}</div>`; return; }
  if (!j.entries.length) { $('#hist').innerHTML = `<div class="empty">${t("history.no_records")}</div>`; return; }
  let html = `<table><thead><tr><th style="width:170px">${t("history.time")}</th><th style="width:170px">${t("history.run_by")}</th><th style="width:200px">${t("history.action")}</th><th>${t("updates.package")}</th></tr></thead><tbody>`;
  for (const e of j.entries) {
    const who = /aptdaemon/.test(e.commandline) ? `aptdaemon<div class="sub1">${t("history.dashboard_or_this_tool")}</div>` :
      /apt-get|apt /.test(e.commandline) ? `apt-get<div class="sub1">${esc(e.commandline.replace(/^\/usr\/bin\//,''))}</div>` : esc(e.commandline || '—');
    const badges = e.actions.map(a => `<span class="badge ${a.action}">${a.action} ${a.count}</span>`).join('');
    const all = e.actions.flatMap(a => a.names);
    const shown = all.slice(0, 8).map(n => `<span class="pk">${esc(n)}</span>`).join('');
    const more = all.length > 8 ? `<details><summary class="sub1">${t("history.more", {v0: all.length - 8})}</summary>${all.slice(8).map(n => `<span class="pk">${esc(n)}</span>`).join('')}</details>` : '';
    html += `<tr><td class="mono">${esc(e.start)}</td><td>${who}</td><td>${badges}</td><td>${shown}${more}</td></tr>`;
  }
  $('#hist').innerHTML = html + `</tbody></table>`;
}

