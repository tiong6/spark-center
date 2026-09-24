async function showHardware() {      // 硬體分頁：靜態清單只渲染一次（伺服器快取 1 小時＋快照），動態欄位抓一次；只有 USB 埠每 2 秒偵測插拔
  if (!(await ensureStatic())) return;
  if (!hw.staticRendered) { renderHwStatic(); hw.staticRendered = true; }
  pollHw(true);
  pollPorts(); hw.portTimer = setInterval(pollPorts, 2000);
}
$('#btnHwReload').onclick = async () => { const b = $('#btnHwReload'); b.disabled = true; b.textContent = t("updates.loading"); const j = await api('/api/hardware?force=1'); if (j.ok) { hw.static = j; renderHwStatic(); } b.disabled = false; b.textContent = t("hw.reload_hardware"); pollHw(true); pollPorts(); };
/* ---- 後面板示意圖：USB-C 四孔（DP 輸出 + USB 裝置）、HDMI、10GbE、QSFP。
   實體孔 ↔ 控制器 的對應存在伺服器端（data/usbc-map.json），所有瀏覽器共用。
   slot 0 依規格是電源輸入孔；核心沒有 UCSI/typec，供電偵測不到，只能手動標記。 ---- */
hw.rearPrev = null; hw.rearFlash = {}; hw.calibSlot = null; hw.calibMode = false;
const PANEL_ZH = { left: t("hw.left"), right: t("hw.right"), back: t("hw.rear"), front: t("hw.front"), top: t("hw.top"), bottom: t("hw.bottom"), unknown: t("hw.unknown") };
const ICON = {
  usbc: '<svg viewBox="0 0 64 36"><rect class="jk" x="8" y="11" width="48" height="14" rx="7"/></svg>',
  hdmi: '<svg viewBox="0 0 64 36"><path class="jk" d="M10 10 h44 v10 l-6 6 h-32 l-6 -6 z"/></svg>',
  rj45: '<svg viewBox="0 0 64 36"><path class="jk" d="M14 6 h36 v24 h-10 v-5 h-16 v5 h-10 z"/></svg>',
  qsfp: '<svg viewBox="0 0 64 36"><rect class="jk" x="4" y="9" width="56" height="18" rx="2"/><rect class="jk" x="12" y="14" width="40" height="8" rx="1"/></svg>',
  klock: '<svg viewBox="0 0 64 36"><ellipse class="jk" cx="32" cy="18" rx="9" ry="6"/></svg>',
  plug: '<svg viewBox="0 0 64 36"><rect class="jk" x="8" y="11" width="48" height="14" rx="7"/><path d="M26 6 v-4 M38 6 v-4" stroke="#7a827c" stroke-width="3"/></svg>',
};
const spd = v => v >= 5000 ? v / 1000 + ' Gb/s' : v + ' Mb/s';
async function pollRear() {
  const j = await api('/api/hardware/rear');
  if (!j.ok) { $('#rear').innerHTML = `<div class="empty">${t("apps.could_not_load", {v0: esc(j.error)})}</div>`; return; }
  const calib = j.calib || {}; const now = Date.now();
  const sig = Object.fromEntries(j.controllers.map(c => [c.controller, c.devices.map(d => d.path).join(',')]));
  let newly = null;
  if (hw.rearPrev) for (const k in sig) if (hw.rearPrev[k] !== undefined && hw.rearPrev[k] !== sig[k] && sig[k]) { newly = k; hw.rearFlash[k] = now; }
  hw.rearPrev = sig;
  // 校準模式外，若某個「推測」孔的控制器出現新裝置，視為使用者已插入確認 → 升級為 plug
  if (newly && hw.calibSlot == null) { for (const k in calib) if (calib[k] && calib[k].controller === newly && calib[k].source === 'inferred') { await api('/api/hardware/usbc-map', {slot: +k, controller: newly, source: 'plug', note: t("hw.confirmed_by_plugging_in_a_device")}); return pollRear(); } }
  if (newly && hw.calibSlot != null) { const slot = hw.calibSlot; hw.calibSlot = null; await api('/api/hardware/usbc-map', {slot, controller: newly, source: 'plug'}); return pollRear(); }
  const ctrl = Object.fromEntries(j.controllers.map(c => [c.controller, c]));
  const dispByCtrl = k => j.usbc.find(u => u.controller_guess === k)?.display;  // 控制器 0k ↔ DP 輸出 USB-C-k（驅動編號一致，視為同一孔）
  const assigned = new Set(Object.values(calib).map(v => v && v.controller).filter(Boolean));
  const slots = [];
  slots.push(`<div class="slot k-lock">${ICON.klock}<div class="lb">Kensington</div><div class="st"><span class="sub1">—</span></div></div>`);
  for (let i = 0; i < 4; i++) {
    const cal = calib[String(i)]; const cid = cal ? cal.controller : null; const c = cid ? ctrl[cid] : null; const disp = cid ? dispByCtrl(cid) : null;
    const isPower = i === 0;
    const on = !!((c && c.devices.length) || (disp && disp.connected));
    const flash = cid && hw.rearFlash[cid] && now - hw.rearFlash[cid] < 6000;
    let st = '';
    if (isPower) st += `<b>${t("hw.power_supply")}</b><span class="sub1">${t("hw.240_w_pd_input")}</span>`;
    if (disp && disp.connected) st += `<b>${t("hw.display", {v0: esc(disp.mode || ''), v1: disp.hz ? ' @ ' + Math.round(disp.hz) + ' Hz' : ''})}</b>`;
    if (c && c.devices.length) { const dd = c.devices.filter(d => d.bus === 'usb3').concat(c.devices.filter(d => d.bus === 'usb2')); const d0 = dd[0]; const mw = Math.max(...c.devices.map(d => d.max_w || 0));
      st += `<b>${esc(d0.name)}</b>` + (c.devices.some(d => d.children) ? `<span class="sub1">${t("hw.downstream_devices", {v0: c.devices.reduce((a, d) => a + d.children, 0)})}</span>` : '') + `<span class="sub1"> · ${mw ? t("hw.device_declares_ma_w_during_enumeration", {v0: Math.round(mw*200), v1: mw}) : t("hw.self_powered_declares_0_ma")}</span>`; }
    if (!st) st = cid ? `<span class="sub1">${t("hw.empty")}</span>` : `<span class="sub1">${t("hw.not_calibrated")}</span>`;
    const SRC = { plug: t("hw.confirmed_by_plugging_in"), manual: t("hw.manually_reported"), inferred: t("hw.confirmed_on_insertion") };
    // 已確認的孔不再貼標籤；只有推測、未校準才提示
    const tag = isPower ? '' : cal ? (cal.source === 'inferred' ? t("hw.estimated", {v0: SRC.inferred}) : '') : t("hw.not_calibrated");
    const sub2 = cid ? `${esc(cid.replace('NVDA8000:', t("hw.controller")))} · USB-C-${cid.slice(-1)}` : (isPower ? t("hw.controller_unavailable") : t("hw.controller_awaiting_calibration"));
    slots.push(`<div class="slot ${isPower ? 'k-power' : 'k-usbc'} ${on || isPower ? 'on' : ''} ${flash ? 'flash' : ''} ${hw.calibSlot === i ? 'pick' : ''}" data-slot="${i}">${isPower ? ICON.plug : ICON.usbc}<div class="lb"><b class="kind">${isPower ? t("hw.power") : 'USB-C'}</b>${t("hw.port", {v7: i + 1})}<br>${sub2}</div><div class="st">${st}</div>${tag ? `<span class="tg">${tag}</span>` : ''}</div>`);
  }
  const h = j.hdmi; slots.push(`<div class="slot k-hdmi ${h && h.connected ? 'on' : ''}">${ICON.hdmi}<div class="lb"><b class="kind">HDMI</b> 2.1a<br>HDMI-0</div><div class="st">${h ? (h.connected ? `<b>${t("hw.display_2", {v0: esc(h.mode || '')})}</b>` : `<span class="sub1">${t("hw.empty")}</span>`) : `<span class="sub1">${t("hw.xrandr_unavailable")}</span>`}</div></div>`);
  const e = j.eth10g; slots.push(`<div class="slot k-lan ${e && e.carrier === '1' ? 'on' : ''}">${ICON.rj45}<div class="lb"><b class="kind">10GbE</b> LAN<br>${e ? esc(e.iface) : '—'}</div><div class="st">${e ? (e.carrier === '1' ? `<b>${t("hw.connected", {v0: e.speed && e.speed > 0 ? spd(+e.speed) : ''})}</b>` : `<span class="sub1">${t("hw.cable_disconnected")}</span>`) : `<span class="sub1">${t("hw.not_detected")}</span>`}</div></div>`);
  const x = j.connectx7; slots.push(`<div class="slot k-qsfp ${x.ifaces.length ? 'on' : ''}">${ICON.qsfp}<div class="lb"><b class="kind">QSFP</b> · ConnectX-7</div><div class="st">${x.ifaces.length ? `<b>${esc(x.ifaces.join(', '))}</b>` : x.pci_present ? `<span class="sub1">${t("hw.device_present_no_interface")}</span>` : `<span class="sub1">${t("hw.connectx_7_not_found_by_lspci")}</span>`}</div><span class="tg">${t("hw.mlx5_driver", {v3: x.driver_loaded ? t("llm.loaded") : t("hw.not_loaded")})}</span></div>`);
  // 還沒對應到任何孔、但接著東西的控制器，不能藏起來
  // 只算四個外露孔的控制器（00–03）；04 與 8001:00 韌體標「後方」，是內部（藍牙模組）
  const orphan = j.controllers.filter(c => /^NVDA8000:0[0-3]$/.test(c.controller) && !assigned.has(c.controller) && (c.devices.length || (dispByCtrl(c.controller) || {}).connected));
  const orphanHtml = orphan.length ? `<div class="sub1" style="margin-top:10px">${t("hw.controllers_with_connected_devices_but_no", {v0: orphan.map(c => `${esc(c.controller.replace('NVDA8000:', t("hw.controller")))}（${c.devices.map(d => esc(d.name)).join('、') || t("hw.display_3")}）`).join('；')})}</div>` : '';
  const legend = `<div class="lgd"><span><i style="background:#e5484d"></i>${t("hw.power")}</span><span><i style="background:#76b900"></i>${t("hw.usb_c_data")}</span><span><i style="background:#3583d6"></i>HDMI</span><span><i style="background:#f5a524"></i>10GbE</span><span><i style="background:#a78bfa"></i>QSFP</span><span class="sub1">${t("hw.solid_border_connected_faint_border_empty")}</span></div>`;
  $('#rear').innerHTML = `<div class="chassis"><div class="vents"></div><div class="slots">${slots.join('')}</div></div>${legend}${orphanHtml}`;
  $('#rear').querySelectorAll('.slot[data-slot]').forEach(el => el.onclick = () => { if (hw.calibMode && +el.dataset.slot !== 0) { hw.calibSlot = +el.dataset.slot; pollRear(); } });
  const n = Object.keys(calib).filter(k => k !== '0' && calib[k].source !== 'inferred').length;
  $('#usbPortsSub').textContent = hw.calibMode ? (hw.calibSlot == null ? t("hw.calibrating_click_a_usb_c_port") : t("hw.calibrating_plug_a_device_into_usb", {v0: hw.calibSlot + 1})) :
    t("hw.left_to_right_kensington_usb_c", {v0: n < 3 ? t("hw.3_data_ports_calibrated", {v0: n}) : '', v1: j.displays_available ? '' : t("hw.xrandr_unavailable_display_status_unknown")});
}
async function pollPorts() { return pollRear(); }
$('#btnCalib').onclick = () => { hw.calibMode = !hw.calibMode; hw.calibSlot = null; $('#btnCalib').textContent = hw.calibMode ? t("hw.finish_calibration") : t("hw.calibrate_ports"); pollRear(); };
$('#btnCalibReset').onclick = async () => { if (!confirm(t("hw.clear_all_port_mappings_including_the"))) return; await api('/api/hardware/usbc-map', {reset: true}); hw.calibSlot = null; pollRear(); };
function stopHw() { if (hw.timer) { clearInterval(hw.timer); hw.timer = null; } if (hw.wifiTimer) { clearInterval(hw.wifiTimer); hw.wifiTimer = null; } if (hw.portTimer) { clearInterval(hw.portTimer); hw.portTimer = null; } if (hw.llmTimer) { clearInterval(hw.llmTimer); hw.llmTimer = null; } if (hw.llmFast) { clearInterval(hw.llmFast); hw.llmFast = null; } }
function renderHwStatic() {
  const j = hw.static, S = j.system, C = j.cpu, G = j.gpu, D = j.dmi || {available:false};
  $('#hwNote').textContent = t("hw.static_data_is_cached_for_60_with_value", {value: t("hw.hardware_data_is_cached_for_1_with_value", {value: D.available ? t("hw.serial_numbers_and_memory_modules_come") : ''})}) + (D.available ? '' : ' ' + (D.note || ''));
  // 關於這台機器：每一格都是實際讀到的值，讀不到就「—」
  const disk0 = (j.disks || []).find(d => (d.partitions || []).some(p => (p.mountpoints || []).includes('/'))) || (j.disks || [])[0];
  const family = D.available && D.system.family ? D.system.family : '';
  const machineSvg = `<svg class="machine" viewBox="0 0 150 110" fill="none" stroke="var(--muted)" stroke-width="1.5"><rect x="15" y="20" width="120" height="70" rx="10" fill="#1a1c1b"/><rect x="15" y="20" width="120" height="70" rx="10" fill="url(#hg)"/><defs><linearGradient id="hg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".06"/><stop offset="1" stop-color="#000" stop-opacity=".25"/></linearGradient></defs><path d="M25 32h100M25 40h100M25 48h100M25 56h100M25 64h100M25 72h100" stroke="#2a2e2c" stroke-dasharray="1 3"/><rect x="30" y="80" width="90" height="3" rx="1.5" fill="var(--accent)" stroke="none" opacity=".85"/><circle cx="127" cy="28" r="2.5" fill="var(--accent)" stroke="none"/></svg>`;
  $('#hwHero').classList.remove('hide');
  $('#hwHero').innerHTML = `<div class="about">${machineSvg}<div style="flex:1;min-width:0">
    <div class="hname">${dash(S.sys_vendor).replace(/^ASUSTeK COMPUTER INC\.$/i, 'ASUS')} ${dash(S.product_name)}<small>${[family ? t("hw.series_with_value", {value: family}) : '', S.product_version ? esc(S.product_version) : '', S.hostname ? t("hw.hostname_with_value", {value: esc(S.hostname)}) : ''].filter(Boolean).join(' · ')}</small></div>
    <div class="specs">
      <div><b>${G ? dash(G.name) : '—'}</b><span>${t("hw.chip", {v5: C ? t("hw.cores", {v0: dash(C.cpus), v1: C.clusters.map(k => esc(k.model.replace('Cortex-', ''))).join('+')}) : ''})}</span></div>
      <div><b>${GB(j.memory.total)}</b><span>${t("hw.unified_memory", {v7: D.available && D.memory.modules[0] ? ` · ${esc(D.memory.modules[0].type || '')}` : ''})}</span></div>
      <div><b>${disk0 ? GB(disk0.size) : '—'}</b><span>${disk0 ? `${esc((disk0.transport || '').toUpperCase())} · ${esc(disk0.model || '')}` : t("hw.storage")}</span></div>
      <div><b>DGX OS ${dash(S.dgx_release)}</b><span>${t("hw.kernel", {v11: dash(S.os), v12: dash(S.kernel)})}</span></div>
      <div><b class="mono" style="font-size:14px">${D.available ? dash(D.system.serial) : '—'}</b><span>${t("hw.serial_number", {v14: D.available ? '' : t("hw.requires_dmidecode")})}</span></div>
      <div><b>${dash(S.bios_version).split('.').slice(0, 2).join('.')}</b><span>BIOS · ${dash(S.bios_date)}</span></div>
    </div></div></div>`;
  const sys = `<div class="panel"><div class="panel-h"><h2>${t("hw.system")}</h2></div><dl class="kv">
    <dt>${t("hw.model")}</dt><dd>${dash(S.sys_vendor)} ${dash(S.product_name)}<div class="sub1">${dash(S.product_version)}</div></dd>
    <dt>${t("hw.motherboard")}</dt><dd>${dash(S.board_vendor)} ${dash(S.board_name)}${D.available && (D.board.version || D.board.serial) ? `<div class="sub1">${[D.board.version ? t("hw.version_with_value", {value: esc(D.board.version)}) : '', D.board.serial ? t("hw.serial_number_2_with_value", {value: esc(D.board.serial)}) : ''].filter(Boolean).join(' · ')}</div>` : ''}</dd>
    ${D.available ? `<dt>${t("hw.serial_number_3")}</dt><dd class="mono">${dash(D.system.serial)}${D.system.sku ? `<div class="sub1">SKU ${esc(D.system.sku)}</div>` : ''}</dd>
    <dt>UUID</dt><dd class="mono" style="font-size:12px">${dash(D.system.uuid)}</dd>
    <dt>${t("hw.chassis")}</dt><dd>${dash(D.chassis.type)}</dd>` : `<dt>${t("hw.serial_number_3")}</dt><dd class="sub1">${t("hw.requires_dmidecode_root_not_authorized")}</dd>`}
    <dt>BIOS</dt><dd>${dash(S.bios_version)}<div class="sub1">${dash(S.bios_vendor)} · ${dash(S.bios_date)}</div></dd>
    <dt>${t("hw.operating_system")}</dt><dd>${dash(S.os)}<div class="sub1">DGX OS ${dash(S.dgx_release)} · Dashboard ${dash(S.dgx_dashboard)}</div></dd>
    <dt>${t("hw.kernel_2")}</dt><dd class="mono">${dash(S.kernel)}</dd>
    <dt>${t("hw.hostname_2")}</dt><dd>${dash(S.hostname)}</dd>
    <dt>${t("hw.uptime")}</dt><dd id="hwUptime">—</dd>
    <dt>${t("hw.load")}</dt><dd id="hwLoad" class="mono">—</dd></dl></div>`;
  const cpu = C ? `<div class="panel"><div class="panel-h"><h2>${t("hw.processor")}</h2></div><dl class="kv">
    <dt>${t("hw.architecture")}</dt><dd>${dash(C.architecture)} · ${dash(C.vendor)}</dd>
    <dt>${t("hw.cores_2")}</dt><dd>${dash(C.cpus)}</dd>
    ${C.clusters.map(k => `<dt>${esc(k.model)}</dt><dd>${t("hw.cores_up_to", {v1: dash(k.cores), v2: k.max_mhz ? (parseFloat(k.max_mhz)/1000).toFixed(2)+' GHz' : '—'})}</dd>`).join('')}
    <dt>L2 / L3</dt><dd>${dash(C.l2)} / ${dash(C.l3)}</dd></dl></div>` : `<div class="panel"><h2>${t("hw.processor")}</h2><div class="empty">${t("hw.lscpu_unavailable")}</div></div>`;
  const mem = `<div class="panel"><div class="panel-h"><h2>${t("hw.memory")}</h2></div>
    <div class="stat"><div><b id="hwMemUsed">—</b><span>${t("hw.used")}</span></div><div><b>${GB(j.memory.total)}</b><span>${t("disk.total")}</span></div><div><b id="hwSwap">—</b><span>${t("hw.swap_used", {v1: GB(j.memory.swap_total)})}</span></div></div>
    <div id="hwMemMeter"></div>
    ${G && G.unified_memory ? `<div class="sub1" style="margin-top:8px">${t("hw.gb10_uses_a_unified_memory_architecture")}</div>` : ''}</div>`;
  const dimm = D.available ? `<div class="panel"><div class="panel-h"><h2>${t("hw.memory_modules")}</h2><span class="sub">${t("hw.maximum_slots", {v0: dash(D.memory.max_capacity), v1: dash(D.memory.slots), v2: D.memory.error_correction ? ' · ' + esc(D.memory.error_correction) : ''})}</span></div>` +
    (D.memory.modules.length ? `<table><thead><tr><th style="width:80px">${t("hw.slot")}</th><th style="width:80px">${t("hw.capacity")}</th><th>${t("hw.specifications")}</th><th>${t("hw.manufacturer_part_number")}</th></tr></thead><tbody>` +
      D.memory.modules.map(m => `<tr><td class="mono">${dash(m.slot)}</td><td>${dash(m.size)}</td><td>${dash(m.type)}${m.form ? ' · ' + esc(m.form) : ''}<div class="sub1">${m.speed ? esc(m.speed) : '—'}${m.configured_speed && m.configured_speed !== m.speed ? t("hw.actual_with_value", {value: esc(m.configured_speed)}) : ''}</div></td><td>${dash(m.manufacturer)}<div class="sub1">${m.part ? esc(m.part) : t("hw.part_number_not_provided")}</div></td></tr>`).join('') + `</tbody></table>` : `<div class="empty">${t("hw.dmidecode_did_not_report_any_modules")}</div>`) + `</div>` : '';
  const gpu = G ? `<div class="panel"><div class="panel-h"><h2>GPU</h2></div><dl class="kv">
    <dt>${t("hw.model_2")}</dt><dd>${dash(G.name)}</dd>
    <dt>${t("hw.driver_cuda")}</dt><dd>${dash(G.driver)} / ${dash(G.cuda)}</dd>
    <dt>VBIOS</dt><dd class="mono">${dash(G.vbios)}</dd>
    <dt>PCI</dt><dd class="mono">${dash(G.bus)}</dd>
    <dt>${t("hw.maximum_sm_clock")}</dt><dd>${dash(G.max_sm_mhz)}</dd></dl>
    <div class="stat"><div><b id="gpuTemp">—</b><span>${t("hw.temperature")}</span></div><div><b id="gpuUtil">—</b><span>${t("hw.utilization")}</span></div><div><b id="gpuPower">—</b><span>${t("hw.power_2")}</span></div><div><b id="gpuClk">—</b><span>${t("hw.sm_clock")}</span></div></div>
    <div id="gpuMeter"></div></div>` : `<div class="panel"><h2>GPU</h2><div class="empty">${t("hw.nvidia_smi_unavailable")}</div></div>`;
  const N = j.nvme;
  const nvmeHtml = !N ? '' : `<div class="panel"><div class="panel-h"><h2>${t("hw.nvme_health")}</h2><span class="sub">${t("hw.firmware", {v0: esc(N.model), v1: esc(N.firmware), v2: N.available ? ' · ' + esc(N.source) : ''})}</span></div>` +
    (N.available ? `${N.warnings.length ? `<div class="panel notice danger" style="margin-top:10px;padding:10px 14px"><b>${t("hw.controller_warnings")}</b>${N.warnings.map(esc).join('、')}</div>` : `<div class="sub1" style="margin-top:8px"><span class="tag ok">${t("hw.no_warnings")}</span> critical_warning = 0</div>`}
      <div class="stat" style="margin-top:12px"><div><b>${N.percent_used ?? '—'} %</b><span>${t("hw.life_used_manufacturer_estimate")}</span></div><div><b>${N.avail_spare ?? '—'} %</b><span>${t("hw.available_spare_blocks_threshold", {v3: N.spare_thresh ?? '—'})}</span></div><div><b>${N.temp_c ?? '—'} °C</b><span>${t("hw.temperature")}</span></div><div><b>${N.power_on_hours != null ? t("hw.days_with_value", {value: (N.power_on_hours / 24).toFixed(0)}) : '—'}</b><span>${t("hw.powered_on_for_hours", {v6: N.power_on_hours ?? '—'})}</span></div></div>
      ${meter(N.percent_used || 0, 70, 90)}
      <dl class="kv"><dt>${t("hw.total_written")}</dt><dd>${GB(N.data_written)}${N.data_written ? `<span class="sub1">（${(N.data_written / 1e12).toFixed(2)} TB）</span>` : ''}</dd><dt>${t("hw.total_read")}</dt><dd>${GB(N.data_read)}</dd><dt>${t("hw.power_cycles")}</dt><dd>${dash(N.power_cycles)}</dd><dt>${t("hw.unsafe_shutdowns")}</dt><dd>${dash(N.unsafe_shutdowns)}${N.unsafe_shutdowns ? `<span class="sub1">${t("hw.times_power_loss_or_forced_shutdown")}</span>` : ''}</dd><dt>${t("hw.media_errors")}</dt><dd>${dash(N.media_errors)}</dd><dt>${t("hw.error_log_entries")}</dt><dd>${dash(N.num_err_log_entries)}</dd><dt>${t("hw.time_above_temperature_limits")}</dt><dd>${t("hw.min_warning_min_critical", {v16: dash(N.warning_temp_time), v17: dash(N.critical_comp_time)})}</dd><dt>${t("hw.serial_number_3")}</dt><dd class="mono">${esc(N.serial)}</dd></dl>
      <div class="sub1" style="margin-top:8px">${t("hw.life_percentage_is_the_manufacturer_s")}</div>`
    : `<dl class="kv"><dt>${t("hw.serial_number_3")}</dt><dd class="mono">${esc(N.serial)}</dd><dt>${t("updates.status")}</dt><dd>${dash(N.state)}</dd></dl><div class="sub1" style="margin-top:8px">${esc(N.note)}</div>`) + `</div>`;
  const disks = `<div class="panel"><div class="panel-h"><h2>${t("hw.storage")}</h2></div>` + (j.disks ? j.disks.map(d => `
    <dl class="kv"><dt>${esc(d.name)}</dt><dd>${dash(d.model)}<div class="sub1">${GB(d.size)} · ${d.transport ? esc(d.transport.toUpperCase()) : '—'} · ${d.rotational ? 'HDD' : 'SSD'}</div>${d.name === 'nvme0n1' && N ? `<div class="sub1">${t("hw.manufacturer")}<b style="color:var(--text)">${esc(N.vendor || '—')}</b>${N.oui_vendor ? `（PCI ${esc(N.vendor_pci_id)}，EUI OUI ${esc(N.oui)}）` : ''}${N.firmware ? t("updates.firmware", {v0: esc(N.firmware)}) : ''}</div><div class="sub1">${t("hw.controller_2", {v3: esc((N.controller || '—').replace(/ \[.*$/, '')), v4: N.dramless ? t("hw.dram_less", {v0: N.hmb ? t("hw.uses_hmb_to_borrow_host_memory") : ''}) : ''})}</div><div class="sub1">${esc(N.form_factor_note)}；M.2 2242</div>` : ''}</dd></dl>
    ${d.partitions.filter(p => p.usage).map(p => { const u = p.usage, pct = Math.round((u.total - u.free) / u.total * 100); return `
      <div style="margin-top:10px"><span class="mono">${esc(p.name)}</span> <span class="sub1">${t("hw.used_2", {v1: p.mountpoints.join(', '), v2: esc(p.fstype||''), v3: GB(u.total - u.free), v4: GB(u.total), v5: pct})}</span>${meter(pct, 85, 95)}</div>`; }).join('')}`).join('') : `<div class="empty">${t("hw.lsblk_unavailable")}</div>`) + `</div>`;
  const net = `<div class="panel"><div class="panel-h"><h2>${t("hw.network")}</h2></div>` + (j.network ? `<table><thead><tr><th>${t("hw.interface")}</th><th style="width:64px">${t("updates.status")}</th><th style="width:118px">${t("hw.address")}</th><th style="width:56px">${t("hw.rate")}</th></tr></thead><tbody>` +
    j.network.filter(n => n.kind !== 'virtual' || (n.state === 'UP' && n.ipv4.length)).map(n => `<tr><td><span class="mono">${esc(n.name)}</span> <span class="tag">${n.kind}</span><div class="sub1 mono" style="font-size:11px">${dash(n.mac)}</div></td><td><span class="tag ${n.state==='UP'?'ok':''}">${dash(n.state)}</span></td><td class="mono" style="white-space:nowrap;font-size:12px">${n.ipv4.map(esc).join('<br>') || '—'}</td><td class="sub1">${n.speed_mbps ? (n.speed_mbps >= 1000 ? n.speed_mbps/1000 + ' Gb/s' : n.speed_mbps + ' Mb/s') : '—'}</td></tr>`).join('') + `</tbody></table><div class="sub1" style="margin-top:8px">${t("hw.wi_fi_and_disconnected_interfaces_do")}</div>` : `<div class="empty">${t("hw.ip_unavailable")}</div>`) + `</div>`;
  const B = j.bluetooth;
  const bt = `<div class="panel"><div class="panel-h"><h2>${t("hw.bluetooth")}</h2>${B && B.available ? `<span class="sub">${esc(B.controller.address||'')} · ${B.controller.powered === 'yes' ? t("hw.on") : t("hw.off")}${B.rfkill_blocked ? t("hw.blocked_by_rfkill") : ''}</span>` : ''}</div>` +
    (!B ? `<div class="empty">${t("hw.bluetoothctl_unavailable")}</div>` : !B.available ? `<div class="empty">${esc(B.note)}</div>` :
    (B.devices.length ? `<table><thead><tr><th>${t("updates.device")}</th><th style="width:64px">${t("hw.type")}</th><th style="width:78px">${t("updates.status")}</th><th style="width:56px">${t("hw.battery")}</th></tr></thead><tbody>` +
      B.devices.map(d => `<tr><td><b>${esc(d.name)}</b><div class="sub1 mono" style="font-size:11px">${esc(d.mac)}</div></td><td class="sub1" style="white-space:nowrap" title="${esc(d.icon||'')}">${esc(BT_KIND[d.icon] || d.icon || '—')}</td><td>${d.connected ? `<span class="tag ok">${t("hw.connected_2")}</span>` : `<span class="tag">${t("hw.paired")}</span>`}</td><td>${d.battery == null ? '<span class="sub1">—</span>' : d.battery + ' %'}</td></tr>`).join('') + `</tbody></table><div class="sub1" style="margin-top:8px">${t("hw.battery_level_is_available_only_when")}</div>` : `<div class="empty">${t("hw.no_paired_devices")}</div>`)) + `</div>`;
  const sens = `<div class="panel"><div class="panel-h"><h2>${t("hw.sensors")}</h2><span class="sub">${t("hw.fan_speed_hwmon_nvml_and_acpi")}</span></div><div id="hwSensors"><div class="empty">${t("updates.loading")}</div></div></div>`;
  const U = j.usb_tree;
  const USB_IC = { roothub: '⌂', hub: '⊞', hid: '⌨', storage: '▤', wireless: '◉', audio: '♪', video: '▣', printer: '⎙', comm: '⇄', billboard: '⊡', vendor: '?', other: '•', image: '▣' };
  const usbNode = d => {
    const sub = [d.manufacturer, `${d.vid}:${d.pid}`, d.speed_label, d.max_ma ? t("hw.declares_ma", {v0: d.max_ma}) : (d.max_ma === 0 ? t("hw.self_powered") : ''), d.interfaces.map(i => i.driver).filter(Boolean).filter((v, i, a) => a.indexOf(v) === i).map(x => t("hw.driver_with_value", {value: x})).join(' ') || t("hw.no_driver"), d.serial ? 'S/N ' + d.serial : '', d.kind === 'hub' || d.kind === 'roothub' ? t("hw.ports", {v0: d.ports}) : ''].filter(Boolean).join(' · ');
    const node = `<div class="node"><span class="ic ${d.kind}">${USB_IC[d.kind] || '•'}</span><div><div class="nm">${esc(d.name)}${d.name_from_ids ? `<span class="tag" title="${t("hw.device_did_not_report_a_name")}">usb.ids</span>` : ''}<span class="kind">${esc(d.kind_label)}</span></div><div class="sub1">${esc(sub)}</div></div></div>`;
    if (d.children.length) return `<li><details open><summary>${node}</summary><ul>${d.children.map(usbNode).join('')}</ul></details></li>`;
    return `<li class="leaf">${node}</li>`;
  };
  const usb = `<div class="panel" style="grid-column: 1 / -1"><div class="panel-h"><h2>${t("hw.usb_devices")}</h2><span class="sub">${U ? t("hw.nodes_including_empty_root_hubs_collapsed", {v0: U.total, v1: U.empty_roothubs, v2: U.ids_file ? '＋usb.ids' : ''}) : '—'}</span></div>` +
    (U ? (U.roots.length ? `<div class="tree"><ul>${U.roots.map(usbNode).join('')}</ul></div>` : `<div class="empty">${t("hw.no_usb_devices_connected")}</div>`) : (j.usb ? `<pre class="raw">${esc(j.usb.join('\n'))}</pre>` : `<div class="empty">${t("hw.sys_bus_usb_is_unreadable")}</div>`)) + `</div>`;
  const P = j.pci_devices;
  const genLbl = (g, w) => g ? `PCIe ${g}.0 ×${w ?? '?'}` : '—';
  const pci = `<div class="panel" style="grid-column: 1 / -1"><div class="panel-h"><h2>${t("hw.pci_devices")}</h2><span class="sub">${P ? t("hw.devices_bridges_collapsed_source_lspci_sys", {v0: P.devices.length, v1: P.bridges}) : '—'}</span></div>` +
    (P ? `<table><thead><tr><th style="width:26%">${t("updates.device")}</th><th style="width:16%">${t("hw.type")}</th><th style="width:20%">${t("hw.link_current_maximum")}</th><th style="width:14%">${t("hw.bandwidth")}</th><th style="width:12%">${t("hw.driver_2")}</th><th>${t("hw.address")}</th></tr></thead><tbody>` +
      P.devices.map(d => { const down = d.cur_gen && d.max_gen && (d.cur_gen < d.max_gen || (d.cur_width || 0) < (d.max_width || 0)); const isGpu = d.kind === 'GPU';
        return `<tr><td><b>${esc(d.friendly)}</b><div class="sub1 mono" style="font-size:11px">${esc(d.vid)}:${esc(d.did)}${d.subsystem ? ' · ' + esc(d.subsystem) : ''}</div></td><td>${esc(d.kind)}</td><td class="mono" style="font-size:12px">${genLbl(d.cur_gen, d.cur_width)}<span class="sub1"> / ${genLbl(d.max_gen, d.max_width)}</span>${isGpu ? `<div class="sub1">${t("hw.built_into_the_soc_pcie_values")}</div>` : down ? `<div class="sub1" style="color:var(--warn)">${t("hw.below_maximum_normal_slowdown_when_idle")}</div>` : ''}</td><td class="sub1">${d.cur_gbps != null && !isGpu ? `${d.cur_gbps} GB/s` : '—'}</td><td class="mono" style="font-size:12px">${esc(d.driver || t("llm.none"))}</td><td class="mono sub1" style="font-size:11px">${esc(d.slot)}</td></tr>`; }).join('') +
      `</tbody></table>` +
      (P.empty_ports.length ? `<div class="sub1" style="margin-top:10px">${t("hw.another_root_ports_have_no_connected", {v0: P.empty_ports.length, v1: P.empty_ports.map(e => t("hw.maximum", {v0: esc(e.slot), v1: genLbl(e.max_gen, e.max_width)})).join('、'), v2: P.empty_ports.some(e => e.max_gen === 5 && e.max_width === 4) ? t("hw.the_two_empty_gen5_4_ports") : ''})}</div>` : '') +
      `<details style="margin-top:8px"><summary class="sub1">${t("hw.raw_lspci_output")}</summary><pre class="raw">${esc((j.pci || []).join('\n'))}</pre></details>`
    : (j.pci ? `<pre class="raw">${esc(j.pci.join('\n'))}</pre>` : `<div class="empty">${t("hw.lspci_unavailable")}</div>`)) + `</div>`;
  const PR = j.printers;
  const printer = !PR ? '' : `<div class="panel"><div class="panel-h"><h2>${t("common.printer")}</h2><span class="sub">${t("hw.cups_sources_lpstat_driverless_mdns_usb", {v0: PR.cups_active ? t("hw.running") : t("hw.not_running")})}</span></div>` +
    (PR.queues.length ? `<table><thead><tr><th>${t("hw.queue")}</th><th style="width:80px">${t("updates.status")}</th><th>${t("hw.connection")}</th></tr></thead><tbody>${PR.queues.map(q => `<tr><td><b>${esc(q.name)}</b>${q.default ? `<span class="tag ok">${t("hw.default")}</span>` : ''}</td><td><span class="tag ${q.state === 'idle' ? 'ok' : ''}">${esc(q.state)}</span></td><td class="mono sub1" style="font-size:11px">${esc(q.uri || '—')}</td></tr>`).join('')}</tbody></table><div class="sub1" style="margin-top:6px">${t("hw.queued_jobs", {v1: PR.jobs})}</div>` : `<div class="sub1" style="margin-top:8px">${t("hw.no_printers_configured_in_cups")}</div>`) +
    (PR.discovered.length ? `<div style="margin-top:10px"><b>${t("hw.found_on_the_local_network_not")}</b><table><tbody>${PR.discovered.map(d => `<tr><td>${esc(d.name)}</td><td class="mono sub1" style="font-size:11px">${esc(d.uri)}</td></tr>`).join('')}</tbody></table><div class="sub1">${t("hw.use")}<span class="mono">${t("hw.lpadmin_p_name_e_v_uri")}</span>${t("hw.to_add_one_ipp_everywhere_requires")}</div></div>` : `<div class="sub1" style="margin-top:6px">${t("hw.no_printers_found_through_mdns_ipp")}</div>`) +
    (PR.usb.length ? `<div class="sub1" style="margin-top:6px">${t("hw.usb_printers", {v0: PR.usb.map(u => esc((u.manufacturer ? u.manufacturer + ' ' : '') + u.name)).join('、')})}</div>` : `<div class="sub1">${t("hw.no_usb_printers_connected")}</div>`) + `</div>`;
  $('#hw').innerHTML = sys + cpu + mem + dimm + gpu + disks + nvmeHtml + net + bt + printer + sens + usb + pci;
}
