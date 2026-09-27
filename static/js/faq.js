/* 常見問題：NVIDIA 團隊在論壇置頂維護的〈DGX Spark / GB10 FAQ〉整理版。
   題目文字在 i18n.js（faq.<id>.q／faq.<id>.a），用輕量標記：「1. 」「- 」開頭是清單、「## 」是小標、`反引號` 是程式碼；
   指令與連結跟語言無關，放在這裡。「本機」框是 /api/faq 即時讀的，和 NVIDIA 的說法分開畫；
   原文是抄進來的快照，有沒有改版由 /api/faq/source 查，查不到就講查不到。 */
const FAQ_SOURCE = { url: 'https://forums.developer.nvidia.com/t/dgx-spark-gb10-faq/347344', version: 11, updated: '2026-08-04' };
const FAQ_CATS = ['setup', 'network', 'memory', 'display', 'software', 'cluster', 'security'];
const FAQ_APPLE_LAN = ['faq.link.apple_lan', 'https://support.apple.com/guide/mac-help/control-access-to-your-local-network-on-mac-mchla4f49138/mac'];
const FAQ = [
  { id: 'appliance', cat: 'setup', links: [
    ['faq.link.ms_network', 'https://support.microsoft.com/en-us/windows/essential-network-settings-and-tasks-in-windows-f21a9bbc-c582-55cd-35e0-73431160a1b9'],
    ['faq.link.ms_sharing', 'https://support.microsoft.com/en-us/windows/file-sharing-over-a-network-in-windows-b58704b2-f53a-4b82-7bc1-80f9994725bf'],
    FAQ_APPLE_LAN] },
  { id: 'ssid', cat: 'setup', fe: true },
  { id: 'power_on', cat: 'setup' },
  { id: 'mac_ssh', cat: 'network', links: [FAQ_APPLE_LAN] },
  { id: 'sync_exists', cat: 'network', cmds: [
    ['Windows', 'C:\\Users\\<username>\\AppData\\Local\\NVIDIA Corporation\\Sync\\config\\ssh_config'],
    ['macOS', '/Users/<username>/Library/Application Support/NVIDIA/Sync/config/ssh_config'],
    ['Linux', '/home/<username>/.config/NVIDIA/Sync/config/ssh_config']] },
  { id: 'moving', cat: 'network', local: 'wifi', cmds: [
    ['faq.cmd.wifi_connect', 'sudo nmcli d wifi connect <wifi_name> password <password>'],
    ['faq.cmd.wifi_ip', 'ip -f inet a show wlP9s9']] },
  { id: 'smi_memory', cat: 'memory', local: 'mem', cmds: [['faq.cmd.mem_check', 'free -h']] },
  { id: 'mem_issues', cat: 'memory', local: 'cache', cmds: [
    ['faq.cmd.check_cache', 'free -h'],
    ['faq.cmd.drop_caches', "sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'"]] },
  { id: 'display', cat: 'display' },
  { id: 'docker', cat: 'software', links: [['faq.link.docker', 'https://docs.nvidia.com/dgx/dgx-spark/nvidia-container-runtime-for-docker.html']] },
  { id: 'nims', cat: 'software', links: [['faq.link.build', 'https://build.nvidia.com/spark']] },
  { id: 'nvcc_openmp', cat: 'software', cmds: [
    ['faq.cmd.cmake', 'target_compile_options(mytarget PRIVATE\n    $<$<COMPILE_LANGUAGE:CUDA>:-Xcompiler=-fopenmp>\n)'],
    ['faq.cmd.nvcc', 'nvcc -Xcompiler=-fopenmp ...']] },
  { id: 'nemoclaw', cat: 'software', links: [
    ['faq.link.nemoclaw', 'https://github.com/NVIDIA/NemoClaw/discussions'],
    ['faq.link.nemoclaw_faq', 'https://github.com/NVIDIA/NemoClaw/discussions/categories/q-a?discussions_q=category%3AQ%26A+is%3Aclosed+author%3AzNeill+'],
    ['faq.link.openshell', 'https://github.com/NVIDIA/OpenShell/discussions']] },
  { id: 'cx7', cat: 'cluster', local: 'cx7', cmds: [
    ['faq.cmd.hotplug_off', 'sudo rm -f /etc/nvidia/cx7-hotplug-enabled'],
    ['faq.cmd.hotplug_on', 'sudo touch /etc/nvidia/cx7-hotplug-enabled']] },
  { id: 'stacking', cat: 'cluster', links: [
    ['faq.link.two', 'https://build.nvidia.com/spark/connect-two-sparks'],
    ['faq.link.three', 'https://build.nvidia.com/spark/connect-three-sparks'],
    ['faq.link.switch', 'https://build.nvidia.com/spark/multi-sparks-through-switch'],
    ['faq.link.nccl', 'https://build.nvidia.com/spark/nccl']] },
  { id: 'qsfp', cat: 'cluster', links: [['faq.link.qsfp', 'https://marketplace.nvidia.com/en-us/enterprise/personal-ai-supercomputers/qsfp-cable-0-4m-for-dgx-spark/']] },
  { id: 'rdma', cat: 'cluster', links: [['faq.link.ibv', 'https://man7.org/linux/man-pages/man3/ibv_reg_mr.3.html']] },
  { id: 'bios_pw', cat: 'security', local: 'vendor' },
];
const faq = { built: false, local: null, machine: null, dash: null, open: false };

/* 輕量標記 → HTML。先整段 esc 再套標記，字串表裡的 <username> 之類不會變成標籤 */
function faqRich(src) {
  const inline = s => esc(s).replace(/`([^`]+)`/g, '<code>$1</code>');
  let html = '', list = null;
  const close = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const line of src.split('\n')) {
    const m = line.match(/^(\d+\.|-) (.*)$/);
    if (m) {
      const kind = m[1] === '-' ? 'ul' : 'ol';
      if (list !== kind) { close(); html += `<${kind}>`; list = kind; }
      html += `<li>${inline(m[2])}</li>`;
      continue;
    }
    close();
    if (!line.trim()) continue;
    const h = line.match(/^## (.*)$/);
    html += h ? `<h4>${inline(h[1])}</h4>` : `<p>${inline(line)}</p>`;
  }
  close();
  return html;
}

function faqItem(it) {
  const q = t(`faq.${it.id}.q`), a = t(`faq.${it.id}.a`);
  const label = l => l.startsWith('faq.') ? t(l) : l;
  const tags = (it.local ? `<span class="tag ok">${t('faq.tag_local')}</span>` : '')
    + (it.fe ? `<span class="tag" title="${esc(t('faq.tag_fe_hint'))}">${t('faq.tag_fe')}</span>` : '');
  const cmds = (it.cmds || []).map(([l, cmd]) =>
    `<div class="fcmd"><div class="sub1">${esc(label(l))}</div><div class="fbox"><pre>${esc(cmd)}</pre><button class="small" data-copy>${t('faq.copy')}</button></div></div>`).join('');
  const links = it.links ? `<div class="flinks"><span class="sub1">${t('faq.links')}</span>`
    + it.links.map(([k, href]) => `<a href="${esc(href)}" target="_blank" rel="noopener">${esc(t(k))} ↗</a>`).join('') + `</div>` : '';
  const local = it.local ? `<div class="floc"><div class="sub1"><b>${t('faq.local_title')}</b> · ${t('faq.local_hint')}</div>`
    + `<dl class="kv" id="faqLoc-${it.id}"><dt>…</dt><dd class="muted">${t('faq.loading')}</dd></dl></div>` : '';
  const search = [q, a, ...(it.cmds || []).map(c => c[1])].join('\n').toLowerCase();
  return `<details class="faq" data-id="${it.id}" data-search="${esc(search)}"><summary><span class="fq">${esc(q)}</span>${tags}</summary>`
    + `<div class="fa">${faqRich(a)}${cmds}${links}${local}</div></details>`;
}

/* 本機框：每個值都來自 /api/faq、/api/machine、/api/dashboard；讀不到就寫讀不到 */
function faqLocalRows(kind) {
  const L = faq.local, row = (k, v) => `<dt>${esc(k)}</dt><dd>${v}</dd>`;
  const na = k => row(t(k), esc(t('faq.l.unreadable')));
  if (kind === 'vendor') {
    const v = faq.machine && faq.machine.ok ? faq.machine.vendor : '';
    return v ? row(t('faq.l.vendor'), esc(/^nvidia/i.test(v) ? t('faq.l.vendor_nvidia') : t('faq.l.vendor_oem', {v}))) : na('faq.l.vendor');
  }
  if (!L || !L.ok) return na('faq.local_title');
  if (kind === 'cx7') {
    const c = L.cx7;
    if (!c) return na('faq.l.cx7_visible');
    // 判讀只用兩個讀得到的事實（旗標檔、PCI 上有沒有 0x15b3），不猜線有沒有插：CX7 沒通電時從軟體看不到線
    const [cls, key] = c.visible > 0 ? ['ok', 'faq.l.cx7_powered'] : c.hotplug ? ['ok', 'faq.l.cx7_expected'] : ['warn', 'faq.l.cx7_odd'];
    return row(t('faq.l.cx7_hotplug'), esc(t(c.hotplug ? 'faq.l.on_flag' : 'faq.l.off_flag', {path: c.flag})))
      + row(t('faq.l.cx7_visible'), esc(t('faq.l.cx7_count', {n: c.visible})))
      + row(t('faq.l.verdict'), `<span class="fv ${cls}">${esc(t(key))}</span>`);
  }
  if (kind === 'mem' || kind === 'cache') {
    const m = L.mem;
    if (!m) return na('faq.l.memory');
    const avail = m.available == null ? '—' : fmtBytes(m.available);
    if (kind === 'cache') return row(t('faq.l.buff_cache'), esc(t('faq.l.buff_cache_val', {v: fmtBytes(m.buff_cache)}))) + row(t('faq.l.available'), esc(avail));
    const used = m.available == null ? '—' : fmtBytes(m.total - m.available);
    const d = faq.dash;
    const dash = d && d.ok ? (d.installed ? `<a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.url)}</a>` : esc(t('faq.l.not_installed'))) : esc(t('faq.l.unreadable'));
    return row(t('faq.l.memory'), esc(t('faq.l.mem_used', {used, total: fmtBytes(m.total), avail})) + ` · <a href="#monitor" data-goto="monitor">${t('faq.goto_monitor')}</a>`)
      + row(t('faq.l.dashboard'), dash);
  }
  if (kind === 'wifi') {
    const w = L.wifi;
    if (!w) return row(t('faq.l.wifi'), esc(t('faq.l.no_wifi')));
    const st = w.state === 'up' ? (w.ipv4.length ? t('faq.l.wifi_up', {name: w.name, ip: w.ipv4.join(', ')}) : t('faq.l.wifi_noip', {name: w.name}))
      : t('faq.l.wifi_down', {name: w.name});
    return row(t('faq.l.wifi'), esc(st))
      + row(t('faq.l.cmd_note'), esc(w.name === 'wlP9s9' ? t('faq.l.wifi_same') : t('faq.l.wifi_diff', {name: w.name})));
  }
  return '';
}

function renderFaqSource(s) {
  const el = $('#faqRemote');
  if (!s) { el.innerHTML = `<span class="muted">${esc(t('faq.src_checking'))}</span>`; return; }
  if (!s.ok || s.error || !s.version) { el.innerHTML = `<span class="muted">${esc(t('faq.src_error', {err: s.error || '—'}))}</span>`; return; }
  if (s.version === FAQ_SOURCE.version) { el.innerHTML = `<span class="fv ok">✓ ${esc(t('faq.src_same', {v: s.version, time: (s.checked || '—').replace('T', ' ')}))}</span>`; return; }
  el.innerHTML = `<span class="fv warn">${esc(t(s.version > FAQ_SOURCE.version ? 'faq.src_newer' : 'faq.src_other', {v: s.version, date: s.updated || '—'}))}</span>`;
}

function buildFaq() {
  faq.built = true;
  $('#faqBased').textContent = t('faq.src_based', {v: FAQ_SOURCE.version, date: FAQ_SOURCE.updated});
  $('#faqOpen').href = FAQ_SOURCE.url;
  $('#faqList').innerHTML = FAQ_CATS.map(c => {
    const items = FAQ.filter(it => it.cat === c);
    return `<div class="panel faqcat"><div class="panel-h"><h2>${t('faq.cat.' + c)}</h2><span class="sub">${t(items.length === 1 ? 'faq.count_one' : 'faq.count', {n: items.length})}</span></div>${items.map(faqItem).join('')}</div>`;
  }).join('') + `<div class="panel empty hide" id="faqNone"></div>`;
  renderFaqSource(null);
  api('/api/faq/source').then(renderFaqSource);   // 伺服器快取一天；失敗不快取，下次開頁再查
}

async function loadFaq() {
  if (!faq.built) buildFaq();
  [faq.local, faq.machine, faq.dash] = await Promise.all([api('/api/faq'), api('/api/machine'), api('/api/dashboard')]);
  for (const it of FAQ) if (it.local) { const el = document.getElementById('faqLoc-' + it.id); if (el) el.innerHTML = faqLocalRows(it.local); }
}

$('#faqSearch').oninput = e => {
  const raw = e.target.value.trim(), q = raw.toLowerCase();
  let shown = 0;
  document.querySelectorAll('#faqList .faqcat').forEach(p => {
    let n = 0;
    p.querySelectorAll('details.faq').forEach(d => {
      const hit = !q || d.dataset.search.includes(q);
      d.classList.toggle('hide', !hit);
      if (q && hit) d.open = true;   // 搜尋時把命中的打開，看得到命中在哪；清空搜尋不動使用者自己開的
      n += hit;
    });
    p.classList.toggle('hide', !n);
    shown += n;
  });
  const none = $('#faqNone');
  none.classList.toggle('hide', !!shown);
  none.textContent = t('faq.no_match', {q: raw});
};
$('#faqToggle').onclick = () => {
  faq.open = !faq.open;
  document.querySelectorAll('#faqList details.faq:not(.hide)').forEach(d => d.open = faq.open);
  $('#faqToggle').textContent = t(faq.open ? 'faq.collapse_all' : 'faq.expand_all');
};
function faqSelect(pre) {
  const r = document.createRange(); r.selectNodeContents(pre);
  const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  try { return document.execCommand('copy'); } catch (e) { return false; }
}
$('#faqList').addEventListener('click', e => {
  const g = e.target.closest('[data-goto]');
  if (g) { e.preventDefault(); showTab(g.dataset.goto); return; }
  const b = e.target.closest('[data-copy]');
  if (!b) return;
  const pre = b.parentElement.querySelector('pre');
  const done = ok => { b.textContent = t(ok ? 'faq.copied' : 'faq.copy_failed'); setTimeout(() => { b.textContent = t('faq.copy'); }, 1600); };
  // 剪貼簿 API 只在安全環境可用（127.0.0.1、localhost 算）；不行就選取文字再試 execCommand，失敗就說失敗
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(pre.textContent).then(() => done(true), () => done(faqSelect(pre)));
  else done(faqSelect(pre));
});
