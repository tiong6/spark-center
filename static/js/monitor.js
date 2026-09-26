/* ---- 監控尺寸切換（大／小），記在瀏覽器 ---- */
function applyMonSize(sz) {
  $('#mon').classList.toggle('compact', sz === 'compact');
  $('#monSize').querySelectorAll('span').forEach(x => x.classList.toggle('on', x.dataset.size === sz));
  try { localStorage.setItem('spark-center.monSize', sz); } catch (e) {}
}
$('#monSize').querySelectorAll('span').forEach(x => x.onclick = () => applyMonSize(x.dataset.size));
(() => { let sz = 'large'; try { sz = localStorage.getItem('spark-center.monSize') || 'large'; } catch (e) {} applyMonSize(sz); })();

/* ---- Wi-Fi 頻道分析：偶爾用一次的診斷，只在按下時掃描（主動重掃約 8 秒且會讓連線變鈍）---- */
hw.chanOpen = false; hw.chanData = null; hw.chanLoading = false; hw.chanApsOpen = false;
async function loadChannels(rescan) {
  hw.chanLoading = true; const box = $('#chanbox'); if (box) box.innerHTML = renderChannels();
  const j = await api('/api/hardware/wifi/channels' + (rescan ? '?rescan=1' : ''));
  hw.chanLoading = false; hw.chanData = j;
  const b2 = $('#chanbox'); if (b2) { b2.innerHTML = renderChannels(); bindChannelBtns(); }
}
function bindChannelBtns() {
  const r = $('#chanRescan'); if (r) r.onclick = () => loadChannels(true);
  const d = $('#chanAps'); if (d) d.ontoggle = () => { hw.chanApsOpen = d.open; };   // 重繪會重建元素，狀態記在外面
}
function renderChannels() {
  if (hw.chanLoading) return `<div class="sub1">${t("mon.scanning_an_active_rescan_takes_about")}</div>`;
  const j = hw.chanData;
  if (!j) return `<div class="sub1">${t("mon.not_analyzed_yet")}</div>`;
  if (!j.ok) return `<div class="sub1">${t("mon.analysis_failed", {v0: esc(j.error || '')})}</div>`;
  const cur = j.current;
  let h = `<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><span class="sub1">${t("mon.current_scanned_at", {v0: cur ? t("mon.channel", {v0: esc(cur.ssid || ''), v1: cur.chan, v2: esc(cur.band)}) : t("mon.disconnected"), v1: esc(j.scanned.replace('T',' ')), v2: j.rescanned ? t("mon.active_rescan") : t("mon.system_cache")})}</span><button class="small" id="chanRescan">${t("mon.rescan")}</button></div>`;
  for (const [band, d] of Object.entries(j.bands)) {
    const best = d.candidates[0];
    const max = Math.max(1, ...d.candidates.map(c => c.score));
    h += `<div style="margin-top:10px"><b>${esc(band)}</b> <span class="sub1">${t("mon.lower_scores_are_better", {v1: best && cur && cur.band === band ? (best.chan === cur.chan ? t("mon.already_on_the_best_channel") : t("mon.consider_channel", {v0: best.chan})) : ''})}</span>`;
    h += `<table><thead><tr><th style="width:70px">${t("mon.channel_2")}</th><th style="width:130px">${t("mon.interference_score")}</th><th>${t("mon.main_sources")}</th></tr></thead><tbody>` +
      d.candidates.map(c => `<tr class="${c === best ? 'best' : ''} ${cur && cur.band === band && c.chan === cur.chan ? 'now' : ''}"><td>ch${c.chan}${cur && cur.band === band && c.chan === cur.chan ? t("mon.current") : ''}</td><td>${c.score} <i class="bar" style="width:${Math.round(c.score / max * 60)}px"></i></td><td class="sub1">${c.sources.length ? c.sources.map(x => `${esc(x.ssid || t("mon.hidden"))}@ch${x.chan}·${x.signal}%`).join('、') : t("llm.none")}</td></tr>`).join('') +
      `</tbody></table><div class="sub1">${esc(d.note)}</div></div>`;
  }
  h += `<details id="chanAps" style="margin-top:8px" ${hw.chanApsOpen ? 'open' : ''}><summary class="sub1">${t("mon.visible_aps", {v1: j.aps.length})}</summary><table><tbody>` +
    j.aps.map(a => `<tr><td class="sub1" style="width:70px">ch${a.chan}</td><td class="sub1" style="width:60px">${esc(a.band.replace(' GHz','G'))}</td><td>${esc(a.ssid || t("mon.hidden_2"))}${a.in_use ? `<span class="tag ok">${t("mon.current_2")}</span>` : ''}</td><td class="sub1" style="width:60px">${a.signal}%</td><td class="sub1 mono" style="font-size:11px">${esc(a.bssid)}</td></tr>`).join('') +
    `</tbody></table></details>`;
  h += `<div class="sub1" style="margin-top:8px">${j.caveats.map(esc).join(' ')}</div>`;
  return h;
}

/* ---- 即時監控：儀表 + 折線（SVG 手繪，無外部庫）---- */
const MON_INTERVAL_MS = 2000, MON_MAX_POINTS = 150;   // 2 秒一點 → 5 分鐘
const C_LINE = '#3583d6', C_TX = '#c98416'; // 兩色已用 validate_palette 在深色底驗證通過
hw.hist = {}; hw.prev = null;
window.pushHist = function pushHist(key, t, v) { const a = (hw.hist[key] ||= []); a.push([t, v]); if (a.length > MON_MAX_POINTS) a.shift(); }
function sevColor(pct) { return pct >= 90 ? 'var(--danger)' : pct >= 75 ? 'var(--warn)' : 'var(--accent)'; }
function arcPath(cx, cy, r, a0, a1) { // 角度：180=左端, 0=右端（半圓）
  const p = a => [cx + r * Math.cos(Math.PI * a / 180), cy - r * Math.sin(Math.PI * a / 180)];
  const [x0, y0] = p(a0), [x1, y1] = p(a1);
  return `M ${x0.toFixed(1)} ${y0.toFixed(1)} A ${r} ${r} 0 ${(a0 - a1) > 180 ? 1 : 0} 1 ${x1.toFixed(1)} ${y1.toFixed(1)}`;
}
function gaugeSvg(pct) {
  const cx = 100, cy = 105, p = Math.max(0, Math.min(100, pct ?? 0));
  const zone = (from, to, col) => `<path d="${arcPath(cx, cy, 92, 180 - from * 1.8, 180 - to * 1.8)}" stroke="${col}" stroke-width="6" fill="none" stroke-linecap="butt"/>`;
  return `<svg viewBox="0 0 200 120">
    ${zone(0, 75, 'var(--accent)')}${zone(75, 90, 'var(--warn)')}${zone(90, 100, 'var(--danger)')}
    <path d="${arcPath(cx, cy, 72, 180, 0)}" stroke="#3a3f3c" stroke-width="18" fill="none"/>
    ${p > 0 ? `<path d="${arcPath(cx, cy, 72, 180, 180 - p * 1.8)}" stroke="${sevColor(p)}" stroke-width="18" fill="none"/>` : ''}
  </svg>`;
}
function sparkSvg(series, ymax, fmt) {
  // series: [{key, color, data:[[t,v],...]}]；單一 y 軸、上緣標最大值、4 條淡格線、hover 十字線
  const W = 400, H = 110, PAD = 4, n = Math.max(2, ...series.map(s => s.data.length));
  const x = i => PAD + (W - 2 * PAD) * (n <= 1 ? 1 : i / (n - 1));
  const y = v => H - PAD - (H - 2 * PAD) * (ymax > 0 ? Math.min(1, Math.max(0, v / ymax)) : 0);
  let g = '';
  for (let k = 1; k <= 4; k++) g += `<line x1="0" x2="${W}" y1="${(H * k / 4).toFixed(1)}" y2="${(H * k / 4).toFixed(1)}" stroke="#2c302e" stroke-width="1"/>`;
  let paths = '';
  for (const s of series) {
    if (!s.data.length) continue;
    const off = n - s.data.length;
    const pts = s.data.map((d, i) => `${x(i + off).toFixed(1)},${y(d[1]).toFixed(1)}`).join(' ');
    const first = `${x(off).toFixed(1)},${(H - PAD).toFixed(1)}`, last = `${x(n - 1).toFixed(1)},${(H - PAD).toFixed(1)}`;
    paths += `<polygon points="${first} ${pts} ${last}" fill="${s.color}" opacity="0.18"/><polyline points="${pts}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round"/>`;
  }
  return `<div class="ymax">${esc(fmt(ymax))}</div><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${g}${paths}<line class="xh" x1="0" x2="0" y1="0" y2="${H}" stroke="#9aa19a" stroke-width="1" style="display:none"/></svg><div class="tip"></div>`;
}
function bindHover(el, series, fmt) {
  const svg = el.querySelector('svg'), tip = el.querySelector('.tip'), xh = el.querySelector('.xh');
  const n = Math.max(2, ...series.map(s => s.data.length));
  el.onmousemove = e => {
    const r = svg.getBoundingClientRect(); const fx = (e.clientX - r.left) / r.width; const i = Math.round(fx * (n - 1));
    const rows = series.map(s => { const d = s.data[i - (n - s.data.length)]; return d ? `<span style="color:${s.color}">■</span> ${s.label ? s.label + ' ' : ''}${fmt(d[1])}` : null; }).filter(Boolean);
    if (!rows.length) { tip.style.display = 'none'; xh.style.display = 'none'; return; }
    const t = series.map(s => s.data[i - (n - s.data.length)]).find(Boolean)[0];
    tip.innerHTML = `<div class="sub1">${new Date(t * 1000).toLocaleTimeString(LANG)}</div>${rows.join('<br>')}`;
    tip.style.display = 'block'; tip.style.left = (fx * 100) + '%';
    xh.setAttribute('x1', (fx * 400).toFixed(1)); xh.setAttribute('x2', (fx * 400).toFixed(1)); xh.style.display = 'block';
  };
  el.onmouseleave = () => { tip.style.display = 'none'; xh.style.display = 'none'; };
}
const fmtPct = v => v == null ? '—' : Math.round(v) + ' %';
const fmtRate = v => v == null ? '—' : v >= 1048576 ? (v/1048576).toFixed(2) + ' MB/s' : v >= 1024 ? (v/1024).toFixed(1) + ' KB/s' : Math.round(v) + ' B/s';
function monCard(id, title, sub, left, series, ymax, fmt, legend) {
  return `<div class="mcard" id="mon-${id}"><h3><span class="t">${esc(title)}</span>${sub ? `<span class="sub" title="${esc(sub)}">${esc(sub)}</span>` : ''}</h3>${left}<div class="spark" data-k="${id}">${sparkSvg(series, ymax, fmt)}</div>${legend ? `<div class="legend">${legend}</div>` : ''}</div>`;
}
function gaugeLeft(pct, hero, sub) { return `<div class="gauge">${gaugeSvg(pct)}<div class="hero"><b>${esc(hero)}</b></div><div class="sub">${esc(sub)}</div></div>`; }
function kvRight(rows) { return `<dl class="kv" style="margin-top:0;grid-template-columns:90px 1fr">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`; }
function tileLeft(hero, sub) { return `<div class="tile"><b>${esc(hero)}</b><span>${esc(sub)}</span></div>`; }

// 監控版面（順序＋隱藏）：只是這台瀏覽器的偏好，讀不到就當預設，不會影響資料
function monLayout() {
  try { const L = JSON.parse(localStorage.getItem('spark-center.monLayout') || '{}'); return {order: Array.isArray(L.order) ? L.order : [], hidden: Array.isArray(L.hidden) ? L.hidden : []}; } catch (e) { return {order: [], hidden: []}; }
}
function monLayoutSave(L) { try { localStorage.setItem('spark-center.monLayout', JSON.stringify(L)); } catch (e) {} }
function renderMon(j, rerender) {
  const S = hw.static, sampleTime = j.epoch, prev = rerender ? hw.prevPrev : hw.prev, cards = [], hovers = [];
  const pushHist = rerender ? () => {} : window.pushHist;
  // 記憶體
  const m = j.memory, used = m.total - m.available, mpct = used / m.total * 100;
  pushHist('mem', sampleTime, used);
  cards.push(monCard('mem', t("mon.system_memory"), 'MemTotal − MemAvailable', gaugeLeft(mpct, GB(used), GB(m.total) + ' total'), [{key:'mem', color: C_LINE, data: hw.hist.mem}], m.total, GB));
  hovers.push(['mem', [{color: C_LINE, data: hw.hist.mem}], GB]);
  // CPU：兩次 /proc/stat 的差（總和＋每核）
  let cpct = null, corePct = [];
  if (prev && prev.cpu && j.cpu) {
    const dt = j.cpu.total - prev.cpu.total, di = j.cpu.idle - prev.cpu.idle; if (dt > 0) cpct = (1 - di / dt) * 100;
    corePct = j.cpu.cores.map((c, i) => { const p = prev.cpu.cores[i]; if (!p) return null; const d = c.total - p.total; return d > 0 ? (1 - (c.idle - p.idle) / d) * 100 : 0; });
  }
  if (cpct != null) pushHist('cpu', sampleTime, cpct);
  const topo = S.cpu_cores || [], gov = topo[0] && topo[0].governor, allMax = topo.length && topo.every((c, i) => c.max_mhz && j.cpu_freq[i] === c.max_mhz);
  const sw = `<span class="switch"><span class="${hw.cpuView !== 'cores' ? 'on' : ''}" data-cpuview="all">${t("mon.overview")}</span><span class="${hw.cpuView === 'cores' ? 'on' : ''}" data-cpuview="cores">${t("mon.per_core")}</span></span>`;
  const cpuSub = t("mon.load", {v0: S.cpu ? t("mon.cores_with_value", {value: S.cpu.cpus}) : '', v1: j.load ? j.load.map(x => x.toFixed(2)).join(' ') : '—', v2: gov ? t("mon.governor_with_value", {value: gov}) : '', v3: allMax ? t("mon.clock_fixed_at_maximum") : ''});
  if (hw.cpuView === 'cores') {
    const tiles = topo.map((c, i) => { const u = corePct[i], f = j.cpu_freq[i]; const cl = c.max_mhz >= 3500 ? 'X925' : 'A725';
      return `<div class="core" title="${t("mon.cpu_maximum_mhz", {v0: i, v1: cl, v2: c.max_mhz})}"><div class="cn"><span>cpu${i}</span><span>${u == null ? '…' : Math.round(u) + '%'}</span></div><div class="cb"><i style="width:${u == null ? 0 : Math.min(100, u)}%;background:${sevColor(u || 0)}"></i></div><div class="cf"><span>${f == null ? '—' : f + ' MHz'}</span><span class="cl">${cl}</span></div></div>`; }).join('');
    cards.push(`<div class="mcard" id="mon-cpu"><h3><span class="t">${t("mon.cpu_utilization")}</span><span>${esc(cpuSub)}</span>${sw}</h3>${gaugeLeft(cpct, cpct == null ? '…' : fmtPct(cpct), cpct == null ? t("mon.waiting_for_second_sample") : t("mon.average_across_all_cores_2_second"))}<div class="cores">${tiles}</div><div class="legend" style="color:var(--muted)">${t("mon.per_core_temperatures_this_machine_has")}</div></div>`);
  } else {
    cards.push(monCard('cpu', t("mon.cpu_utilization"), cpuSub, gaugeLeft(cpct, cpct == null ? '…' : fmtPct(cpct), cpct == null ? t("mon.waiting_for_second_sample") : t("mon.average_across_all_cores_2_second")), [{color: C_LINE, data: hw.hist.cpu || []}], 100, fmtPct));
    hovers.push(['cpu', [{color: C_LINE, data: hw.hist.cpu || []}], fmtPct]);
  }
  // GPU
  if (j.gpu && S.gpu) {
    const g = j.gpu;
    if (g.util_pct != null) pushHist('gpu', sampleTime, g.util_pct);
    const rs = (g.event_reasons || []).filter(r => r !== t("mon.gpu_idle"));
    cards.push(monCard('gpu', t("mon.gpu_utilization"), t("mon.source", {v0: S.gpu.name, v1: g.pstate ? ' · ' + g.pstate : '', v2: rs.length ? t("mon.slowdown_reasons_with_value", {value: rs.join('、')}) : '', v3: g.source || S.gpu.source || '—'}), gaugeLeft(g.util_pct, fmtPct(g.util_pct), g.source === 'NVML' ? 'NVML utilization.gpu' : 'nvidia-smi utilization.gpu'), [{color: C_LINE, data: hw.hist.gpu || []}], 100, fmtPct));
    hovers.push(['gpu', [{color: C_LINE, data: hw.hist.gpu || []}], fmtPct]);
    if (g.temp_c != null) pushHist('temp', sampleTime, g.temp_c);
    const tmax = S.gpu.throttle_temp_c || 100;
    const tsrc = S.gpu.throttle_source === 'NVML slowdown threshold' ? t("mon.slowdown_threshold_c_actual_nvml_driver", {v0: S.gpu.throttle_temp_c, v1: S.gpu.shutdown_temp_c ? t("mon.shutdown_c", {v0: S.gpu.shutdown_temp_c}) : ''}) : S.gpu.throttle_temp_c ? t("mon.slowdown_threshold_about_c_estimated_from", {v0: S.gpu.throttle_temp_c}) : t("mon.gauge_maximum_100_c_slowdown_threshold");
    cards.push(monCard('temp', t("mon.gpu_temperature"), tsrc, gaugeLeft(g.temp_c == null ? null : g.temp_c / tmax * 100, g.temp_c == null ? '—' : g.temp_c + ' °C', S.gpu.throttle_temp_c ? t("mon.c_below_slowdown_threshold", {v0: (S.gpu.throttle_temp_c - g.temp_c).toFixed(0)}) : ''), [{color: C_LINE, data: hw.hist.temp || []}], tmax, v => Math.round(v) + ' °C'));
    hovers.push(['temp', [{color: C_LINE, data: hw.hist.temp || []}], v => v.toFixed(0) + ' °C']);
    if (g.power_w != null) pushHist('power', sampleTime, g.power_w);
    const ph = hw.hist.power || [], pmax = S.gpu.power_limit_w || Math.max(1, ...ph.map(d => d[1])) * 1.15;
    cards.push(monCard('power', t("mon.gpu_power"), S.gpu.power_limit_w ? t("mon.limit_w", {v0: S.gpu.power_limit_w}) : t("mon.nvidia_smi_does_not_provide_a"), S.gpu.power_limit_w ? gaugeLeft(g.power_w / S.gpu.power_limit_w * 100, g.power_w.toFixed(1) + ' W', t("mon.limit_w", {v0: S.gpu.power_limit_w})) : tileLeft(g.power_w == null ? '—' : g.power_w.toFixed(1) + ' W', t("mon.current_power")), [{color: C_LINE, data: ph}], pmax, v => v.toFixed(1) + ' W'));
    hovers.push(['power', [{color: C_LINE, data: ph}], v => v.toFixed(1) + ' W']);
  }
  // Wi-Fi：訊號儀表（-90 dBm=0%、-30 dBm=100%）＋曲線；速率／MCS／重試率／斷線
  const W = j.wifi;
  if (W && W.connected) {
    const q = Math.max(0, Math.min(100, (W.signal_dbm + 90) / 60 * 100));
    pushHist('wifi', sampleTime, q);
    let retryPct = null;
    if (prev && prev.wifi && prev.wifi.connected && prev.wifi.bssid === W.bssid) { const dp = W.tx_packets - prev.wifi.tx_packets, dr = W.tx_retries - prev.wifi.tx_retries; if (dp > 0) retryPct = dr / dp * 100; }
    hw.wifiRetryPct = retryPct ?? hw.wifiRetryPct ?? null;
    const grade = W.signal_dbm >= -60 ? [t("mon.good"), 'var(--accent)'] : W.signal_dbm >= -70 ? [t("mon.fair"), 'var(--accent)'] : W.signal_dbm >= -80 ? [t("mon.weak"), 'var(--warn)'] : [t("mon.very_poor"), 'var(--danger)'];
    const ex = hw.wifiExtra || {};
    const sameRouter = (ex.aps || []).filter(a => a.ssid && W.ssid && a.ssid.replace(/[_-]?5G$/i, '') === W.ssid.replace(/[_-]?5G$/i, '') && !a.in_use);
    const btNames = ex.bt_connected || [];
    const coex = W.band === '2.4 GHz' && btNames.length ? `<div class="panel notice warn" style="margin-top:8px;padding:8px 12px">${t("mon.wi_fi_is_on_2_4", {v0: btNames.map(esc).join('、'), v1: sameRouter.find(a => a.freq_mhz > 4900) ? t("mon.the_router_has_5_ghz_signal", {v0: esc(sameRouter.find(a => a.freq_mhz > 4900).ssid), v1: sameRouter.find(a => a.freq_mhz > 4900).signal_pct}) : ''})}</div>` : '';
    const weak = W.signal_dbm < -75 ? `<div class="sub1" style="margin-top:6px;color:var(--warn)">${t("mon.signal_dbm_is_weak_limiting_the", {v0: W.signal_dbm, v1: W.tx_mcs ? W.tx_mcs.mcs : '?', v2: hw.wifiRetryPct != null ? t("mon.retry_rate", {v0: hw.wifiRetryPct.toFixed(0)}) : '', v3: sameRouter.length ? t("mon.the_same_router_s_signal_is", {v0: sameRouter.map(a => `${esc(a.ssid)}（${a.freq_mhz > 4900 ? '5 GHz' : '2.4 GHz'}，${a.signal_pct}%）`).join('、')}) : ''})}</div>` : '';
    const kvRow = (k, v, full) => `<div title="${esc(full || '')}"><dt>${k}</dt><dd>${v}</dd></div>`;
    const right = `<dl class="wifikv">
      ${kvRow(t("mon.connected_to"), `${esc(W.ssid)} <span class="sub1">${esc(W.band)} · ch ${W.channel} · ${W.width_mhz} MHz</span>`, t("mon.channel_mhz", {v0: W.ssid, v1: W.band, v2: W.channel, v3: W.width_mhz}))}
      ${kvRow(t("hw.rate"), `↑ ${W.tx_mbps ?? '—'} / ↓ ${W.rx_mbps ?? '—'} Mb/s <span class="sub1">${W.tx_mcs ? `${W.tx_mcs.phy}-MCS ${W.tx_mcs.mcs}` : ''}${W.rx_mcs ? ` / MCS ${W.rx_mcs.mcs}` : ''}</span>`, t("mon.transmit_mb_s_receive_mb_s", {v0: W.tx_mbps ?? '—', v1: W.rx_mbps ?? '—'}))}
      ${kvRow(t("mon.retry_rate_2"), `${hw.wifiRetryPct != null ? hw.wifiRetryPct.toFixed(0) + ' %' : '…'} <span class="sub1">${t("mon.cumulative_failed", {v1: (W.tx_retries / Math.max(1, W.tx_packets) * 100).toFixed(0), v2: W.tx_failed ?? '—'})}</span>`, t("mon.retry_rate_over_the_latest_sampling"))}
      ${kvRow(t("mon.stability"), `${t("mon.beacons_lost_disconnects_in_24h", {v0: W.beacon_loss ?? '—', v1: ex.events_24h ? ex.events_24h.disconnects : '—'})}<span class="sub1">${t("mon.connected_for", {v2: W.connected_s != null ? fmtUptime(W.connected_s) : '—'})}</span>`, t("mon.beacons_lost_disconnects_in_24_hours", {v0: W.beacon_loss ?? '—', v1: ex.events_24h ? ex.events_24h.disconnects : '—', v2: W.connected_s != null ? fmtUptime(W.connected_s) : '—'}))}</dl>`;
    const warns = [coex, weak].filter(Boolean);
    const warnBtn = warns.length ? `<span class="warnbtn ${W.signal_dbm < -80 ? 'danger' : ''}" data-warn="wifi" title="${t("mon.notices_click_to_view", {v1: warns.length})}">⚠</span>` : '';
    const chanBtn = `<button class="small" data-chan="1" style="margin-left:8px">${t("mon.channel_analysis")}</button>`;
    cards.push(`<div class="mcard" id="mon-wifi"><h3><span class="t">Wi-Fi</span><span class="sub">${t("mon.signal", {v0: esc(W.iface)})}<b style="color:${grade[1]}">${grade[0]}</b></span>${warnBtn}${chanBtn}</h3>${gaugeLeft(q, W.signal_dbm + ' dBm', t("mon.average_dbm_transmit_dbm", {v0: W.signal_avg_dbm ?? '—', v1: W.txpower_dbm ?? '—'}))}<div class="spark" data-k="wifi">${sparkSvg([{color: C_LINE, data: hw.hist.wifi || []}], 100, v => (v * 0.6 - 90).toFixed(0) + ' dBm')}</div>${right}${warns.length ? `<div class="warnbox ${hw.warnOpen && hw.warnOpen.wifi ? 'open' : ''}" data-warnbox="wifi">${warns.join('')}</div>` : ''}<div class="chanbox ${hw.chanOpen ? 'open' : ''}" id="chanbox">${renderChannels()}</div></div>`);
    hovers.push(['wifi', [{color: C_LINE, data: hw.hist.wifi || []}], v => (v * 0.6 - 90).toFixed(0) + ' dBm']);
  } else if (W) {
    cards.push(`<div class="mcard" id="mon-wifi"><h3><span class="t">Wi-Fi</span><span class="sub">${esc(W.iface)}</span></h3>${tileLeft(t("mon.disconnected"), '')}<div class="sub1">${t("mon.wi_fi_is_not_connected_to")}</div></div>`);
  }
  // NVMe 溫度（hwmon nvme Composite，免 root）
  const nv = (j.sensors || []).find(x => x.chip === 'nvme' && /composite/i.test(x.label));
  if (nv) { pushHist('nvtemp', sampleTime, nv.temp_c);
    cards.push(monCard('nvtemp', t("mon.nvme_temperature"), t("mon.hwmon_nvme_composite_gauge_maximum_85"), gaugeLeft(nv.temp_c / 85 * 100, nv.temp_c.toFixed(0) + ' °C', t("mon.ssd_controller_composite_temperature")), [{color: C_LINE, data: hw.hist.nvtemp || []}], 85, v => Math.round(v) + ' °C'));
    hovers.push(['nvtemp', [{color: C_LINE, data: hw.hist.nvtemp || []}], v => v.toFixed(1) + ' °C']); }
  // 磁碟用量卡已移除：用量幾分鐘內不變，磁碟分頁與硬體分頁都有
  // 網路：每個有 IPv4 且非虛擬的介面，rx/tx 速率
  // 狀態與速度看即時資料（j.net），介面清單（種類、IP）看硬體快取；快取說 DOWN 但即時說 UP（剛插上網路線）就重讀一次硬體資料拿 IP
  const live = n => j.net[n.name] || {};
  const ifs = (S.network || []).filter(n => n.kind !== 'virtual' && ((live(n).state || n.state) === 'UP' || live(n).state === 'up'));
  const stale = ifs.filter(n => n.state !== 'UP' && !hw.netRefreshed);
  if (stale.length) { hw.netRefreshed = true; api('/api/hardware?force=1').then(r => { if (r.ok) hw.static = r; }); }
  for (const n of ifs) {
    const c = j.net[n.name]; if (!c) continue;
    const mbps = c.speed_mbps || n.speed_mbps;
    const speedTxt = mbps ? (mbps >= 1000 ? ` · ${mbps / 1000} Gb/s` : ` · ${mbps} Mb/s`) : '';
    let rx = null, tx = null;
    if (prev && prev.net[n.name]) { const dt = sampleTime - prev.epoch; if (dt > 0) { rx = (c.rx - prev.net[n.name].rx) / dt; tx = (c.tx - prev.net[n.name].tx) / dt; } }
    if (rx != null) { pushHist('rx:' + n.name, sampleTime, rx); pushHist('tx:' + n.name, sampleTime, tx); }
    const rs = hw.hist['rx:' + n.name] || [], ts = hw.hist['tx:' + n.name] || [];
    const ymax = Math.max(1024, ...rs.map(d => d[1]), ...ts.map(d => d[1])) * 1.15;
    const ser = [{label: t("mon.download"), color: C_LINE, data: rs}, {label: t("mon.upload"), color: C_TX, data: ts}];
    cards.push(monCard('net-' + n.name, (n.kind === 'wifi' ? t("mon.network_wifi") : n.kind === 'ethernet' ? t("mon.network_wired") : t("mon.network", {v0: n.name})) + speedTxt, t("mon.chart_scale_follows_the_interval_maximum", {v0: n.name, v1: n.ipv4.join(', ')}), tileLeft(rx == null ? '…' : '↓ ' + fmtRate(rx), rx == null ? t("mon.waiting_for_second_sample") : '↑ ' + fmtRate(tx)), ser, ymax, fmtRate, `<span><i style="background:${C_LINE}"></i>${t("mon.download_rx")}</span><span><i style="background:${C_TX}"></i>${t("mon.upload_tx")}</span>`));
    hovers.push(['net-' + n.name, ser, fmtRate]);
  }
  // 版面自訂：順序與隱藏記在這個瀏覽器（localStorage），每次重繪都套用；拖曳中不重繪，免得卡片在手上被換掉
  if (hw.dragging) { if (!rerender) { hw.prevPrev = hw.prev; hw.prev = j; } return; }
  const idOf = html => (html.match(/id="mon-([^"]+)"/) || [])[1] || '';
  const order = monLayout().order, hidden = new Set(monLayout().hidden);
  const rank = id => { const i = order.indexOf(id); return i < 0 ? 1e9 : i; };
  const shown = cards.map(h => [idOf(h), h]).filter(([id]) => !hidden.has(id)).sort((a, b) => rank(a[0]) - rank(b[0]) || 0);
  const allIds = cards.map(idOf);
  $('#mon').innerHTML = shown.map(([, h]) => h).join('');
  const nHidden = allIds.filter(id => hidden.has(id)).length;
  const customized = nHidden > 0 || order.length > 0;
  $('#monReset').classList.toggle('hide', !customized);
  $('#monReset').textContent = t("mon.reset_layout") + (nHidden ? `（${t("mon.hidden_n", {n: nHidden})}）` : '');
  $('#monReset').onclick = () => { monLayoutSave({order: [], hidden: []}); if (hw.prev) renderMon(hw.prev, true); };
  document.querySelectorAll('#mon .mcard').forEach(card => {
    const h3 = card.querySelector('h3'); if (!h3) return;
    if (!h3.querySelector('.hidebtn')) h3.insertAdjacentHTML('beforeend', `<span class="hidebtn" title="${t("mon.hide_card")}">×</span>`);
    h3.querySelector('.hidebtn').onclick = e => { e.stopPropagation(); const L = monLayout(); L.hidden = [...new Set([...L.hidden, card.id.slice(4)])]; monLayoutSave(L); if (hw.prev) renderMon(hw.prev, true); };
    const tt = h3.querySelector('.t'); if (tt) tt.title = t("mon.drag_hint");
    card.draggable = true;
    card.addEventListener('dragstart', e => {
      if (!hw.dragArmed) { e.preventDefault(); return; }
      hw.dragging = card; card.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; try { e.dataTransfer.setData('text/plain', card.id); } catch (_) {}
    });
    card.addEventListener('dragover', e => {
      if (!hw.dragging || hw.dragging === card) return;
      e.preventDefault();
      const kids = [...card.parentNode.children], from = kids.indexOf(hw.dragging), to = kids.indexOf(card);
      card.parentNode.insertBefore(hw.dragging, from < to ? card.nextSibling : card);
    });
    card.addEventListener('dragend', () => {
      card.classList.remove('dragging'); hw.dragArmed = false;
      const L = monLayout(); L.order = [...document.querySelectorAll('#mon .mcard')].map(c => c.id.slice(4)); monLayoutSave(L);
      hw.dragging = null; if (hw.prev) renderMon(hw.prev, true);
    });
    h3.addEventListener('mousedown', e => { hw.dragArmed = !!e.target.closest('.t'); });
  });
  hw.warnOpen = hw.warnOpen || {};
  document.querySelectorAll('.warnbtn[data-warn]').forEach(b => b.onclick = () => { const k = b.dataset.warn; hw.warnOpen[k] = !hw.warnOpen[k]; const box = document.querySelector(`.warnbox[data-warnbox="${k}"]`); if (box) box.classList.toggle('open', hw.warnOpen[k]); });
  document.querySelectorAll('button[data-chan]').forEach(b => b.onclick = () => { hw.chanOpen = !hw.chanOpen; const box = $('#chanbox'); if (box) box.classList.toggle('open', hw.chanOpen); if (hw.chanOpen && !hw.chanData) loadChannels(false); });
  bindChannelBtns();
  const cpuH3 = document.querySelector('#mon-cpu h3'); if (cpuH3 && !cpuH3.querySelector('.switch')) cpuH3.insertAdjacentHTML('beforeend', sw);
  document.querySelectorAll('[data-cpuview]').forEach(el => el.onclick = () => { hw.cpuView = el.dataset.cpuview; if (hw.prev) renderMon(hw.prev, true); });
  for (const [k, ser, fmt] of hovers) { const el = document.querySelector(`.spark[data-k="${k}"]`); if (el) bindHover(el, ser, fmt); }
  $('#monNote').title = t("mon.sampled_every_seconds_the_chart_keeps", {v0: MON_INTERVAL_MS/1000, v1: MON_MAX_POINTS, v2: Math.round(MON_INTERVAL_MS*MON_MAX_POINTS/60000)});
  if (!rerender) { hw.prevPrev = hw.prev; hw.prev = j; }
}

function renderGpuAlert(a) {
  const el = $('#gpuAlert'); if (!el) return;
  $('#dotMon').classList.toggle('hide', !a);
  if (!a) { el.classList.add('hide'); return; }
  el.classList.remove('hide');
  el.innerHTML = `<b>${t("mon.gpu_stuck_in_a_low_power")}</b>${t("mon.since", {v0: esc((a.since || '').replace('T', ' '))})}<div style="margin-top:6px">${esc(a.message)}</div><div class="sub1" style="margin-top:8px"><b>${t("mon.how_to_resolve")}</b>${esc(a.advice)}</div><div class="sub1" style="margin-top:6px">${t("mon.based_on_gpu_utilization_20_and", {v3: a.reasons && a.reasons.length ? t("mon.current_flags", {v0: a.reasons.map(esc).join('、')}) : ''})}</div>`;
}
const setTxt = (sel, v) => { const el = $(sel); if (el) el.textContent = v; };
const setHtml = (sel, v) => { const el = $(sel); if (el) el.innerHTML = v; };
async function pollHw(staticOnly) {
  const j = await api('/api/hardware/live');
  if (!j.ok || !hw.static) return;
  renderGpuAlert(j.gpu_alert);
  if (!staticOnly) { try { renderMon(j); } catch (e) { console.error(e); } }
  const m = j.memory, used = m.total - m.available, pct = Math.round(used / m.total * 100);
  setTxt('#hwMemUsed', GB(used) + ` (${pct}%)`);
  setTxt('#hwSwap', GB(m.swap_total - m.swap_free));
  setHtml('#hwMemMeter', meter(pct));
  setTxt('#hwUptime', fmtUptime(j.uptime_s));
  setTxt('#hwLoad', j.load ? j.load.map(x => x.toFixed(2)).join('  ') : '—');
  if (j.gpu && hw.static.gpu) {
    const g = j.gpu;
    setTxt('#gpuTemp', g.temp_c == null ? '—' : g.temp_c + ' °C');
    setTxt('#gpuUtil', g.util_pct == null ? '—' : g.util_pct + ' %');
    setTxt('#gpuPower', g.power_w == null ? '—' : g.power_w.toFixed(1) + ' W');
    setTxt('#gpuClk', g.sm_mhz == null ? '—' : g.sm_mhz + ' MHz');
    setHtml('#gpuMeter', meter(g.util_pct));
  }
  const sens = j.sensors || [];
  setHtml('#hwSensors', sens.length ? `<table><thead><tr><th style="width:30%">${t("mon.chip")}</th><th>${t("mon.sensor")}</th><th style="width:90px">${t("hw.temperature")}</th></tr></thead><tbody>` +
    sens.map(s => `<tr><td class="mono">${esc(s.chip)}</td><td>${esc(s.label)}</td><td class="mono" style="color:${s.temp_c >= 85 ? 'var(--danger)' : s.temp_c >= 70 ? 'var(--warn)' : 'inherit'}">${s.temp_c.toFixed(1)} °C</td></tr>`).join('') + `</tbody></table>` : `<div class="empty">${t("mon.hwmon_has_no_temperature_sensors")}</div>`);
}

