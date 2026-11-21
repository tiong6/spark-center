/* 常見問題：NVIDIA 團隊在論壇置頂維護的〈DGX Spark / GB10 FAQ〉整理版。
   題目文字在 i18n.js（faq.<id>.q／faq.<id>.a），用輕量標記：「1. 」「- 」開頭是清單、「## 」是小標、`反引號` 是程式碼；
   指令與連結跟語言無關，放在這裡。「本機」框是 /api/faq 即時讀的，和 NVIDIA 的說法分開畫；
   原文是抄進來的快照，有沒有改版由 /api/faq/source 查，查不到就講查不到。 */
const FAQ_SOURCE = { url: 'https://forums.developer.nvidia.com/t/dgx-spark-gb10-faq/347344', version: 12, updated: '2026-09-29' };
const FAQ_CATS = ['staff', 'setup', 'network', 'memory', 'display', 'software', 'cluster', 'security'];
const FAQ_APPLE_LAN = ['faq.link.apple_lan', 'https://support.apple.com/guide/mac-help/control-access-to-your-local-network-on-mac-mchla4f49138/mac'];
/* staff：不在置頂 FAQ 裡、是 NVIDIA 人員在各討論串的回覆（2026-11-21 讀過的）。每題附出處 [串標題, 網址, 誰, 日期]，
   內容寫的是回覆當天的狀態；「調查中」就寫調查中，不替 NVIDIA 下結論。 */
const FAQ_FIELDIAG = ['faq.link.fieldiag', 'https://docs.nvidia.com/pdf/userguide-dgx-spark-fieldiag.pdf'];
const FAQ = [
  { id: 'reboot_1024', cat: 'staff', local: 'reboot_fix', staff: [
    ['DGX Spark repeatedly rebooting after most recent update', 'https://forums.developer.nvidia.com/t/387397/40', 'zneill (NVIDIA)', '2026-11-03'],
    ['DGX Spark repeatedly rebooting after most recent update', 'https://forums.developer.nvidia.com/t/387397/52', 'zneill (NVIDIA)', '2026-11-04']], cmds: [
    ['faq.cmd.dist_upgrade', 'sudo apt update\nsudo apt dist-upgrade\nsudo reboot'],
    ['faq.cmd.initcall_check', 'dpkg -l nvidia-spark-initcall-bl'],
    ['faq.cmd.unhold', 'sudo apt-mark showhold\nsudo apt-mark unhold <packages listed by the previous command>\nsudo rm /etc/default/grub.d/zz-timeout.cfg\nsudo update-grub\nsudo reboot']] },
  { id: 'asus_ec', cat: 'staff', local: 'vendor', staff: [
    ['ASUS GX10 – capsule firmware update (EC 0x02000007 + SoC 0x03000008) stuck on ASUS logo progress bar for 6+ hours', 'https://forums.developer.nvidia.com/t/386166/3', 'aniculescu (NVIDIA)', '2026-10-27']] },
  { id: 'pd_firmware', cat: 'staff', fe: true, local: 'vendor', staff: [
    ['DGX Spark USB-C PD Firmware Stuck at 0x00000001 in ESRT', 'https://forums.developer.nvidia.com/t/387898/2', 'aniculescu (NVIDIA)', '2026-11-05'],
    ['DGX Spark USB-C PD firmware inventory mismatch after Dashboard update', 'https://forums.developer.nvidia.com/t/389146/2', 'aniculescu (NVIDIA)', '2026-11-17'],
    ['DGX Spark USB-C PD Firmware Stuck at 0x00000001 in ESRT', 'https://forums.developer.nvidia.com/t/387898/4', 'aniculescu (NVIDIA)', '2026-11-20']], cmds: [
    ['faq.cmd.pd_installed', 'sudo nvidia-spark-ota-check installed-versions'],
    ['faq.cmd.pd_smbios', 'sudo dmidecode -t 45'],
    ['faq.cmd.pd_update', 'sudo fwupdmgr update'],
    ['faq.cmd.pd_force', 'sudo fwupdmgr refresh --force\n# DGX Spark SoC firmware — 0x02009e0f\nsudo fwupdmgr reinstall 0681fd3882fb4fdca996e412ec249365f6e85838\n# DGX Spark USB-C Power Delivery Controller firmware — 0x00000519\nsudo fwupdmgr reinstall a6c6b7f79c96a1cc84d9612d804675e0c3d879c4\n# DGX Spark Embedded firmware — 0x03000d07\nsudo fwupdmgr reinstall 5ba76097236b8d0b0e58332b5e22215cdad361c3']] },
  { id: 'fans_temps', cat: 'staff', local: 'sensors', staff: [
    ['New DGX Spark: fans barely speed up at high temps and can\'t be adjusted', 'https://forums.developer.nvidia.com/t/386805/25', 'aniculescu (NVIDIA)', '2026-10-28'],
    ['DGX Spark: Boot To Idle then doing nothing & idle, the unit is unusually warm', 'https://forums.developer.nvidia.com/t/367449/14', 'aniculescu (NVIDIA)', '2026-10-27'],
    ['DGX Spark. low fan speed, high temps, device very hot', 'https://forums.developer.nvidia.com/t/348760/68', 'aniculescu (NVIDIA)', '2026-10-27'],
    ['DGX Spark: log-less hard power-off + implausible ACPI thermal sensor spikes', 'https://forums.developer.nvidia.com/t/386556/7', 'aniculescu (NVIDIA)', '2026-11-05']], cmds: [
    ['faq.cmd.lm_sensors', 'sudo apt install lm-sensors\nsensors'],
    ['faq.cmd.temp_conf', 'chip "acpitz-acpi-*"\n    label temp1 "SoC"\n    label temp2 "CPU 0-4"\n    label temp3 "CPU 5-9"\n    label temp4 "CPU 10-14"\n    label temp5 "CPU 15-19"\n    label temp6 "GPU"\n    label temp7 "Uncore"'],
    ['faq.cmd.temp_conf_run', 'sensors -c temp.conf']], links: [FAQ_FIELDIAG] },
  { id: 'usb_plug', cat: 'staff', staff: [
    ['Possible USB issue (latest system updates vs. KVM issue)', 'https://forums.developer.nvidia.com/t/354607/10', 'aniculescu (NVIDIA)', '2026-10-27']] },
  { id: 'power_240', cat: 'staff', local: 'power', staff: [
    ['DGX Spark Power Clarification', 'https://forums.developer.nvidia.com/t/349668', 'MackenzieNVIDIA', '2025-10-31']] },
  { id: 'unsupported', cat: 'staff', staff: [
    ['RTL8127 stuck at 100 Mbps / flapping on r8127', 'https://forums.developer.nvidia.com/t/385261/5', 'aniculescu (NVIDIA)', '2026-10-27'],
    ['Request: Jetson-style owner keys or a developer mode for DGX Spark firmware', 'https://forums.developer.nvidia.com/t/385314/2', 'aniculescu (NVIDIA)', '2026-10-27'],
    ['Feature request: UEFI option for LPDDR5X data rate on DGX Spark', 'https://forums.developer.nvidia.com/t/386823/6', 'aniculescu (NVIDIA)', '2026-10-26']] },
  { id: 'report_bug', cat: 'staff', staff: [
    ['*** If you have a problem PLEASE read this first ***', 'https://forums.developer.nvidia.com/t/347792', 'MackenzieNVIDIA', '2025-10-15']], cmds: [
    ['faq.cmd.bug_report', 'sudo nvidia-bug-report.sh']], links: [FAQ_FIELDIAG] },
  { id: 'appliance', cat: 'setup', links: [
    ['faq.link.ms_network', 'https://support.microsoft.com/en-us/windows/essential-network-settings-and-tasks-in-windows-f21a9bbc-c582-55cd-35e0-73431160a1b9'],
    ['faq.link.ms_sharing', 'https://support.microsoft.com/en-us/windows/file-sharing-over-a-network-in-windows-b58704b2-f53a-4b82-7bc1-80f9994725bf'],
    FAQ_APPLE_LAN] },
  { id: 'ssid', cat: 'setup', fe: true },
  { id: 'power_on', cat: 'setup' },
  { id: 'airgap', cat: 'setup', since: 12, links: [['faq.link.recovery', 'https://www.nvidia.com/en-us/drivers/dgx-spark-recovery-software/']] },   // since：原文哪一版新增；等於目前版本就標 NEW
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
  { id: 'release_upgrade', cat: 'software', own: true, local: 'release', cmds: [
    ['faq.cmd.release_never', "sudo sed -i 's/^Prompt=.*/Prompt=never/' /etc/update-manager/release-upgrades"],
    ['faq.cmd.release_check', 'grep ^Prompt /etc/update-manager/release-upgrades']], links: [
    ['faq.link.dgxos8_upgrade', 'https://docs.nvidia.com/dgx/dgx-os-8-user-guide/latest/upgrading-the-os.html'],
    ['faq.link.dgxos8_810', 'https://docs.nvidia.com/dgx/dgx-os-8-user-guide/8.1.0/release-notes.html'],
    ['faq.link.dgxos8_800', 'https://docs.nvidia.com/dgx/dgx-os-8-user-guide/8.0.0/release-notes.html'],
    ['faq.link.forum_2604_recovery', 'https://forums.developer.nvidia.com/t/need-help-system-recovery-of-dgx-os-after-ubuntu-26-04-lts-upgrade-corrupted-spark/379046'],
    ['faq.link.forum_2510', 'https://forums.developer.nvidia.com/t/dgx-spark-ubuntu-upgrade-to-25-10/356468'],
    ['faq.link.dgxos7', 'https://docs.nvidia.com/dgx/dgx-os-7-user-guide/additional_software.html']] },
  { id: 'phased', cat: 'software', own: true, local: 'phased', cmds: [
    ['faq.cmd.phased_policy', 'apt-cache policy snapd'],
    ['faq.cmd.phased_sim', 'apt-get -s upgrade | grep -A3 -i "kept back\\|未被升級\\|保留"'],
    ['faq.cmd.phased_conf', 'cat /etc/apt/apt.conf.d/99spark-center-phased']], links: [
    ['faq.link.ubuntu_phased', 'https://wiki.ubuntu.com/PhasedUpdates'],
    ['faq.link.apt_phased', 'https://discourse.ubuntu.com/t/phased-updates-in-apt-in-21-04/20345']] },
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
  const isNew = it.since === FAQ_SOURCE.version;   // 只標「目前這一版」新增的；下一版出來自然退掉
  const tags = (isNew ? `<span class="tag new" title="${esc(t('faq.tag_new_hint', {v: FAQ_SOURCE.version, d: FAQ_SOURCE.updated}))}">${t('faq.tag_new')}</span>` : '')
    + (it.local ? `<span class="tag ok">${t('faq.tag_local')}</span>` : '')
    + (it.own ? `<span class="tag" title="${esc(t('faq.tag_own_hint'))}">${t('faq.tag_own')}</span>` : '')   // 自己整理的題，和 NVIDIA 原文分開標
    + (it.fe ? `<span class="tag" title="${esc(t('faq.tag_fe_hint'))}">${t('faq.tag_fe')}</span>` : '')
    + (it.staff ? `<span class="tag sec" title="${esc(t('faq.tag_staff_hint'))}">${t('faq.tag_staff')}</span>` : '');
  const cmds = (it.cmds || []).map(([l, cmd]) =>
    `<div class="fcmd"><div class="sub1">${esc(label(l))}</div><div class="fbox"><pre>${esc(cmd)}</pre><button class="small" data-copy>${t('faq.copy')}</button></div></div>`).join('');
  const links = it.links ? `<div class="flinks"><span class="sub1">${t('faq.links')}</span>`
    + it.links.map(([k, href]) => `<a href="${esc(href)}" target="_blank" rel="noopener">${esc(t(k))} ↗</a>`).join('') + `</div>` : '';
  const staff = it.staff ? `<div class="flinks"><span class="sub1">${t('faq.staff_src')}</span>`
    + it.staff.map(([title, href, who, date]) => `<a href="${esc(href)}" target="_blank" rel="noopener" title="${esc(title)}">${esc(who)} · ${esc(date)} ↗</a>`).join('') + `</div>` : '';
  const local = it.local ? `<div class="floc"><div class="sub1"><b>${t('faq.local_title')}</b> · ${t('faq.local_hint')}</div>`
    + `<dl class="kv" id="faqLoc-${it.id}"><dt>…</dt><dd class="muted">${t('faq.loading')}</dd></dl></div>` : '';
  const search = [q, a, ...(it.cmds || []).map(c => c[1])].join('\n').toLowerCase();
  return `<details class="faq${isNew ? ' new' : ''}" data-id="${it.id}" data-search="${esc(search)}"><summary><span class="fq">${esc(q)}</span>${tags}</summary>`
    + `<div class="fa">${faqRich(a)}${cmds}${staff}${links}${local}</div></details>`;
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
    const c = L.cx7;   // 和硬體分頁後面板同一份（server.py cx7_state）
    if (!c || c.pci_functions == null) return na('faq.l.cx7_visible');
    // 判讀只用兩個讀得到的事實（旗標檔、PCI 上有沒有 0x15b3），不猜線有沒有插：CX7 沒通電時從軟體看不到線
    const [cls, key] = c.pci_functions > 0 ? ['ok', 'faq.l.cx7_powered'] : c.hotplug ? ['ok', 'faq.l.cx7_expected'] : ['warn', 'faq.l.cx7_odd'];
    return row(t('faq.l.cx7_hotplug'), esc(t(c.hotplug ? 'faq.l.on_flag' : 'faq.l.off_flag', {path: c.flag})))
      + row(t('faq.l.cx7_visible'), esc(t('faq.l.cx7_count', {n: c.pci_functions})))
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
  if (kind === 'release') {
    const r = L.release;   // 和更新分頁的開關同一份（server.py release_prompt.status，照 Ubuntu 的讀法）
    if (!r || r.mode == null) return na('faq.l.release_prompt');
    const note = r.mode === 'malformed' ? esc(r.error || '') : releaseNote(r);
    return row(t('faq.l.release_prompt'), `<span class="fv ${r.mode === 'never' ? 'ok' : 'warn'}">${esc(t('faq.l.release_' + r.mode))}</span>`
        + (note ? ` · ${note}` : '') + ` · <a href="#updates" data-goto="updates">${t('faq.goto_updates')}</a>`)
      + row(t('faq.l.release_file'), releaseSource(r));
  }
  if (kind === 'phased') {
    const p = L.phased;
    if (!p) return na('faq.l.phased_policy');
    const pol = p.policy || {}, mode = pol.mode;
    const polText = mode == null ? t('faq.l.unreadable') : t('updates.phased_policy_' + mode) + (pol.other && pol.other.length ? ` · ${t('updates.phased_policy_other', {files: pol.other.join(', ')})}` : pol.ours ? ` · ${t('updates.phased_policy_ours', {file: pol.file})}` : '');
    const heldText = p.held == null ? t('faq.l.unreadable') : p.held.length ? p.held.map(h => `${h.name} ${h.candidate || ''}${LANG === 'zh-TW' ? '（' : ' ('}${t('updates.phased_tag', {p: h.percent == null ? '—' : h.percent})}${LANG === 'zh-TW' ? '）' : ')'}`).join(LANG === 'zh-TW' ? '、' : ', ') : t('faq.l.phased_none');
    return row(t('faq.l.phased_policy'), esc(polText) + ` · <a href="#updates" data-goto="updates">${t('faq.goto_updates')}</a>`) + row(t('faq.l.phased_held'), esc(heldText));
  }
  if (kind === 'reboot_fix') {
    const r = L.reboot_fix;
    if (!r) return na('faq.l.initcall');
    const pkg = r.package == null ? t('faq.l.unreadable') : r.package ? r.package : t('faq.l.not_installed');
    const ok = !!r.fixed;   // 後端用 apt_pkg.version_compare 算「≥ 1.0-2」，不在前端比字串
    const holds = r.holds == null ? t('faq.l.unreadable') : r.holds.length ? r.holds.join(', ') : t('faq.l.none');
    return row(t('faq.l.initcall'), `<span class="fv ${ok ? 'ok' : 'warn'}">${esc(pkg)}</span>`)
      + row(t('faq.l.cmdline'), r.cmdline == null ? esc(t('faq.l.unreadable')) : `<span class="fv ${r.cmdline ? 'ok' : 'warn'}">${esc(t(r.cmdline ? 'faq.l.cmdline_yes' : 'faq.l.cmdline_no'))}</span>`)
      + row(t('faq.l.kernel'), esc(r.kernel || '—'))
      + row(t('faq.l.holds'), `<span class="fv ${r.holds && r.holds.length ? 'warn' : 'ok'}">${esc(holds)}</span>`)
      + row(t('faq.l.zz_timeout'), `<span class="fv ${r.zz_timeout ? 'warn' : 'ok'}">${esc(t(r.zz_timeout ? 'faq.l.exists' : 'faq.l.absent'))}</span>`);
  }
  if (kind === 'sensors') {
    const s = L.sensors;
    if (!s) return na('faq.l.acpi');
    const split = s.acpitz_chips > 1 && s.temps_per_chip.length === 1 && s.temps_per_chip[0] === 1;
    return row(t('faq.l.lm_sensors'), esc(t(s.lm_sensors ? 'faq.l.installed' : 'faq.l.not_installed')))
      + row(t('faq.l.acpi'), esc(t('faq.l.acpi_layout', {n: s.acpitz_chips, per: s.temps_per_chip.join('/') || '—'})))
      + row(t('faq.l.verdict'), `<span class="fv ${split ? 'warn' : 'ok'}">${esc(t(split ? 'faq.l.temp_conf_split' : 'faq.l.temp_conf_ok'))}</span>`)
      + row('', `<a href="#monitor" data-goto="monitor">${t('faq.goto_monitor')}</a>`);
  }
  if (kind === 'power') {
    const p = L.power;
    if (!p) return na('faq.l.gpu_power');
    return row(t('faq.l.gpu_power'), esc(p.gpu_w == null ? t('faq.l.unreadable') : `${p.gpu_w.toFixed(1)} W`) + ` · <a href="#monitor" data-goto="monitor">${t('faq.goto_monitor')}</a>`)
      + row(t('faq.l.sys_power'), esc(t(p.system_power_sensor ? 'faq.l.sys_power_yes' : 'faq.l.sys_power_no')));
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

/* 原文和本頁依據的版本不同（通常是 NVIDIA 改版了）：分頁標籤亮點，開頁時也查一次（main.js） */
const faqRevised = s => !!(s && s.ok && !s.error && s.version && s.version !== FAQ_SOURCE.version);
function renderFaqDot(s) { const d = $('#dotFaq'); d.classList.toggle('hide', !faqRevised(s)); d.title = t('faq.dot_hint'); }

function renderFaqSource(s) {
  renderFaqDot(s);
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

/* 直接開到某一題：網址 #faq/<id>，或任何分頁裡帶 data-faq="<id>" 的連結 */
function openFaq(id) {
  if ($('#tab-faq').classList.contains('hide')) showTab('faq');   // showTab 會同步建好題目
  const d = [...document.querySelectorAll('#faqList details.faq')].find(e => e.dataset.id === id);
  if (!d) return;
  if ($('#faqSearch').value) { $('#faqSearch').value = ''; $('#faqSearch').dispatchEvent(new Event('input')); }   // 搜尋可能把它藏起來
  d.open = true;
  d.scrollIntoView({block: 'center'});
  d.classList.add('flash'); setTimeout(() => d.classList.remove('flash'), 1600);
  history.replaceState(null, '', '#faq/' + id);
}
document.addEventListener('click', e => {
  const a = e.target.closest('[data-faq]');
  if (a) { e.preventDefault(); openFaq(a.dataset.faq); }
});

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
