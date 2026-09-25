/* NodeSource：準備、確認安裝、取消／回復，各步有獨立狀態。 */
async function loadNodeSource() {
  const box = $('#nodeSource');
  try {
    const j = await api('/api/node');
    if (!j.ok) { box.innerHTML = `<div class="sub1">${esc(j.error)}</div>`; return; }
    const s = j.state, pending = s && s.phase !== 'restored';
    const target = pending ? s.target : j.target;
    const canInstall = pending && ['prepared', 'installing', 'install_failed'].includes(s.phase) && j.source_text === s.replacement && !j.installed.startsWith(s.target + '.');
    let html = `<b>${t('node.title')}</b><div class="sub1">${t('node.current', {version: esc(j.installed), major: j.major})}</div>`;
    if (pending) html += `<div class="panel notice ${s.phase.endsWith('failed') ? 'danger' : 'warn'}" style="margin-top:10px">${t('node.phase_' + s.phase)}<div class="sub1">${t('node.saved', {version: esc(s.old_version)})}</div></div>`;
    if (j.release_error) html += `<div class="sub1">${esc(j.release_error)}</div>`;
    html += '<div class="actions" style="margin-top:10px">';
    if ((!pending || s.phase === 'installed') && j.target > j.major) html += `<button class="small" data-node="prepare" data-target="${j.target}">${t('node.prepare', {major: j.target})}</button>`;
    if (canInstall) html += `<button class="small primary" data-node="install">${t('node.install', {major: target})}</button>`;
    if (pending) html += `<button class="small" data-node="restore">${t(j.installed === s.old_version ? 'node.cancel' : 'node.restore')}</button>`;
    html += `</div><div class="sub1" style="margin-top:8px">${t('node.scope')}</div>`;
    box.innerHTML = html;
    box.querySelectorAll('[data-node]').forEach(b => b.onclick = () => previewNodeSource(b.dataset.node, Number(b.dataset.target || target), b));
  } catch (e) { box.innerHTML = `<div class="sub1">${esc(e.message)}</div>`; }
}

async function previewNodeSource(action, target, button) {
  button.disabled = true;
  try {
    const p = await api('/api/node/preview', {action, target});
    if (!p.ok) { alert(p.error); return; }
    let h = `<p>${t('node.confirm_' + action)}</p><p class="mono">${esc(p.source)}</p>`;
    if (p.before !== p.after) h += `<div class="sub1">${t('node.before')}</div><pre class="cl">${esc(p.before)}</pre><div class="sub1">${t('node.after')}</div><pre class="cl">${esc(p.after)}</pre>`;
    if (p.simulation) h += `<p>${t('node.simulation')}</p><pre class="cl">${esc(p.simulation)}</pre>`;
    if (p.impact.length) h += `<div class="panel notice danger">${t('node.impact')}<ul>${p.impact.map(x => `<li>${esc(x.name)}: ${x.unknown ? t('node.unknown') : esc(x.range)}</li>`).join('')}</ul></div>`;
    h += `<p class="sub1">${t('node.not_snapshot')}</p>`;
    $('#mBody').innerHTML = h;
    $('#mOk').classList.remove('hide');
    $('#mOk').onclick = async () => {
      closeModal();
      try {
        const r = await api('/api/node/action', {action, target, confirmation: p.confirmation});
        if (!r.ok) { alert(r.error); return; }
        startPolling(t('node.title'));
        window.scrollTo({top: 0, behavior: 'smooth'});
      } catch (e) { alert(e.message); }
    };
    openModal(t('node.title'));
  } catch (e) { alert(e.message); }
  finally { button.disabled = false; }
}
