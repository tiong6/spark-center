/* 機身溫度圖：左邊是可拖曳旋轉的機身（canvas 手寫正投影，不用外部套件），右邊是晶片內的溫度區。
   兩種尺度刻意分開：機身那邊畫的是「哪一層在哪」，晶片那邊只依核心群分組、不畫晶粒輪廓——晶粒內的實體排列沒有資料。
   機身內部配置依一篇拆機報導的層次畫（底部進氣、後方排氣、雙風扇＋鰭片＋熱管＋銅板、SSD 在底板下），尺寸與位置都是示意。
   面板在卡片格外面：卡片格每次輪詢整組重畫，canvas 放進去拖到一半會被換掉。 */
const THERMO_KEY = 'spark-center.thermo';
const THERMO_VIEW = {yaw: -0.62, elev: 0.6, explode: 0};
const thermo = Object.assign({data: null, bound: false, drag: null, spin: 0, seen: false, raf: 0, last: 0}, THERMO_VIEW);
try { Object.assign(thermo, JSON.parse(localStorage.getItem(THERMO_KEY) || '{}')); } catch (_) {}

const THERMO_STOPS = [[35, [59, 130, 196]], [55, [63, 179, 127]], [70, [245, 197, 66]], [85, [240, 138, 60]], [100, [229, 72, 77]]];
const THERMO_NODATA = [110, 116, 112];
function heatRgb(c) {
  if (c == null) return THERMO_NODATA;
  const S = THERMO_STOPS;
  if (c <= S[0][0]) return S[0][1];
  for (let i = 1; i < S.length; i++) if (c <= S[i][0]) { const f = (c - S[i - 1][0]) / (S[i][0] - S[i - 1][0]); return S[i][1].map((v, k) => Math.round(S[i - 1][1][k] + (v - S[i - 1][1][k]) * f)); }
  return S[S.length - 1][1];
}
const rgbCss = (rgb, a) => `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${a == null ? 1 : a})`;
const fmtC = c => c == null ? '—' : c.toFixed(1) + ' °C';

/* 機身零件，單位 mm：x 往右、y 由前（0）到後（150）、z 往上。layer 是展開時抬高的層數。 */
const THERMO_LIFT = 24, THERMO_SIZE = [150, 150, 51];
const THERMO_PARTS = [
  {id: 'bottom', box: [0, 0, 0, 150, 150, 2], layer: 0, rgb: [58, 63, 61], alpha: 0.9},
  {id: 'front', box: [0, 0, 2, 150, 3, 47], layer: 0, rgb: [150, 158, 154], alpha: 0.22},
  {id: 'ssd', box: [22, 52, 3, 22, 42, 3], layer: 1, heat: 'nvme'},
  {id: 'board', box: [5, 5, 10, 140, 140, 1.6], layer: 2, rgb: [28, 78, 58]},
  {id: 'soc', box: [55, 40, 11.6, 40, 40, 2.4], layer: 2, heat: 'soc'},
  {id: 'plate', box: [48, 33, 15, 54, 54, 3], layer: 3, rgb: [184, 115, 51]},
  {id: 'pipe1', box: [58, 87, 15.5, 5, 36, 3], layer: 3, rgb: [184, 115, 51]},
  {id: 'pipe2', box: [72.5, 87, 15.5, 5, 36, 3], layer: 3, rgb: [184, 115, 51]},
  {id: 'pipe3', box: [87, 87, 15.5, 5, 36, 3], layer: 3, rgb: [184, 115, 51]},
  {id: 'fins', box: [12, 122, 14, 126, 22, 24], layer: 3, rgb: [141, 148, 145], slats: 30},
  {id: 'fan1', cyl: [40, 70, 22, 26, 12], layer: 4, rgb: [52, 57, 55]},
  {id: 'fan2', cyl: [110, 70, 22, 26, 12], layer: 4, rgb: [52, 57, 55]},
  {id: 'top', box: [0, 0, 49, 150, 150, 2], layer: 5, rgb: [154, 161, 154], alpha: 0.14},
];
const BOX_FACES = [[4, 5, 6, 7, 1], [0, 3, 2, 1, 0.5], [0, 1, 5, 4, 0.86], [2, 3, 7, 6, 0.6], [1, 2, 6, 5, 0.74], [3, 0, 4, 7, 0.68]];   // 頂點序由外面看是逆時針；最後一個數是固定的明暗

function thermoProjector(W, H) {
  const ex = thermo.explode, cy = Math.cos(thermo.yaw), sy = Math.sin(thermo.yaw), ce = Math.cos(thermo.elev), se = Math.sin(thermo.elev);
  const zSpan = THERMO_SIZE[2] + ex * 5 * THERMO_LIFT, scale = Math.min(W / 330, H / (215 + ex * 135));
  return (x, y, z, layer) => {
    const px = x - THERMO_SIZE[0] / 2, py = y - THERMO_SIZE[1] / 2, pz = z + ex * (layer || 0) * THERMO_LIFT - zSpan / 2;
    const x1 = px * cy - py * sy, y1 = px * sy + py * cy;
    return [W / 2 + x1 * scale, H / 2 - (y1 * se + pz * ce) * scale, -y1 * ce + pz * se];   // 第三個是深度：越大越靠近鏡頭
  };
}
const polyArea = pts => pts.reduce((a, p, i) => { const q = pts[(i + 1) % pts.length]; return a + p[0] * q[1] - q[0] * p[1]; }, 0);
function thermoFaces(part, P) {
  const L = part.layer, faces = [];
  if (part.box) {
    const [x, y, z, w, d, h] = part.box;
    const v = [[x, y, z], [x + w, y, z], [x + w, y + d, z], [x, y + d, z], [x, y, z + h], [x + w, y, z + h], [x + w, y + d, z + h], [x, y + d, z + h]].map(p => P(p[0], p[1], p[2], L));
    BOX_FACES.forEach((f, k) => faces.push({pts: f.slice(0, 4).map(i => v[i]), shade: f[4], k}));
    return {faces};
  }
  const [cx, cyl, z, r, h] = part.cyl, N = 30, bot = [], top = [];
  for (let i = 0; i < N; i++) { const a = i / N * Math.PI * 2; bot.push(P(cx + r * Math.cos(a), cyl + r * Math.sin(a), z, L)); top.push(P(cx + r * Math.cos(a), cyl + r * Math.sin(a), z + h, L)); }
  for (let i = 0; i < N; i++) { const k = (i + 1) % N; faces.push({pts: [bot[i], bot[k], top[k], top[i]], shade: 0.55 + 0.3 * Math.abs(Math.cos(i / N * Math.PI * 2 + thermo.yaw + 0.8))}); }
  faces.push({pts: top, shade: 1, cap: true}, {pts: bot.slice().reverse(), shade: 0.5});
  return {faces};
}
/* 畫的先後。零件都是互不相交的軸向盒，任兩個一定在某一軸上分得開：鏡頭在那一軸的哪一側，另一側的就該先畫。
   用零件中心的遠近來排會錯——主機板又大又扁，中心比旁邊的風扇近，就把風扇蓋掉了。 */
function thermoBounds(part) {
  const lift = thermo.explode * part.layer * THERMO_LIFT;
  if (part.box) { const [x, y, z, w, d, h] = part.box; return [[x, x + w], [y, y + d], [z + lift, z + h + lift]]; }
  const [cx, cy, z, r, h] = part.cyl; return [[cx - r, cx + r], [cy - r, cy + r], [z + lift, z + h + lift]];
}
function thermoOrder(parts) {
  const cam = [-Math.sin(thermo.yaw) * Math.cos(thermo.elev), -Math.cos(thermo.yaw) * Math.cos(thermo.elev), Math.sin(thermo.elev)];   // 指向鏡頭的方向
  const items = parts.map(part => ({part, b: thermoBounds(part)}));
  const mustPrecede = (a, b) => [0, 1, 2].some(k => Math.abs(cam[k]) > 1e-6 && (cam[k] > 0 ? a.b[k][1] <= b.b[k][0] : a.b[k][0] >= b.b[k][1]));   // a 一定不會擋到 b
  const out = [];
  while (items.length) {
    let i = items.findIndex(c => !items.some(o => o !== c && mustPrecede(o, c) && !mustPrecede(c, o)));
    if (i < 0) i = 0;
    out.push(items.splice(i, 1)[0].part);
  }
  return out;
}

function thermoDraw() {
  const cv = $('#thermoCanvas'); if (!cv || !cv.clientWidth || !thermo.data) return;
  const D = thermo.data, dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
  const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
  const css = getComputedStyle(document.documentElement), col = n => css.getPropertyValue(n).trim();
  const cText = col('--text'), cMuted = col('--muted'), cLine = col('--line'), cInfo = col('--info');
  const font = px => `${px}px ${getComputedStyle(document.body).fontFamily}`;
  const P = thermoProjector(W, H), temps = {soc: D.soc, nvme: D.nvme};

  // 機殼四個角的立柱：展開時看得出每一層原本疊在哪
  g.strokeStyle = cLine; g.lineWidth = 1; g.setLineDash([3, 4]);
  for (const [x, y] of [[0, 0], [150, 0], [150, 150], [0, 150]]) { const a = P(x, y, 0, 0), b = P(x, y, 51, 5); g.beginPath(); g.moveTo(a[0], a[1]); g.lineTo(b[0], b[1]); g.stroke(); }
  g.setLineDash([]);

  for (const part of thermoOrder(THERMO_PARTS)) {
    const faces = thermoFaces(part, P).faces;
    const rgb = part.heat ? heatRgb(temps[part.heat]) : part.rgb, alpha = part.alpha == null ? 1 : part.alpha;
    for (const f of faces) {
      if (polyArea(f.pts) >= 0) continue;   // 畫布 y 向下，朝鏡頭的面面積為負
      g.beginPath(); f.pts.forEach((p, i) => i ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1])); g.closePath();
      g.fillStyle = rgbCss(rgb.map(v => Math.round(v * f.shade)), alpha); g.fill();
      g.strokeStyle = `rgba(0,0,0,${alpha < 1 ? 0.25 : 0.35})`; g.lineWidth = 0.6; g.stroke();
      if (part.slats && f.k < 4) {   // 鰭片：一片片順著排氣方向（前後向）立著，沿左右排開；左右兩個端面是整片的，不畫線
        const q = f.pts, [a0, a1, b0, b1] = f.k === 1 ? [q[0], q[3], q[1], q[2]] : [q[0], q[1], q[3], q[2]];
        g.strokeStyle = 'rgba(0,0,0,0.42)'; g.lineWidth = 0.7; g.beginPath();
        for (let i = 1; i < part.slats; i++) { const u = i / part.slats; g.moveTo(a0[0] + (a1[0] - a0[0]) * u, a0[1] + (a1[1] - a0[1]) * u); g.lineTo(b0[0] + (b1[0] - b0[0]) * u, b0[1] + (b1[1] - b0[1]) * u); }
        g.stroke();
      }
      if (f.cap && part.cyl) {   // 風扇葉片。轉動是固定速度的示意動畫：沒有轉速讀值，快慢不代表任何量測，也不跟溫度連動
        const [cx, cyl, z, r, h] = part.cyl; g.strokeStyle = 'rgba(255,255,255,0.28)'; g.lineWidth = 1;
        for (let i = 0; i < 9; i++) { const a = i / 9 * Math.PI * 2 + thermo.spin, p0 = P(cx + 6 * Math.cos(a), cyl + 6 * Math.sin(a), z + h, part.layer), p1 = P(cx + (r - 3) * Math.cos(a + 0.55), cyl + (r - 3) * Math.sin(a + 0.55), z + h, part.layer); g.beginPath(); g.moveTo(p0[0], p0[1]); g.lineTo(p1[0], p1[1]); g.stroke(); }
      }
    }
  }
  // 有讀值的兩個零件各加一圈光暈，蓋在最上面：機殼沒展開時也看得到熱在哪
  const scaleNow = Math.hypot(P(1, 0, 0, 0)[0] - P(0, 0, 0, 0)[0], P(1, 0, 0, 0)[1] - P(0, 0, 0, 0)[1]) || 1;
  for (const [key, x, y, z, L, rad] of [['soc', 75, 60, 14, 2, 34], ['nvme', 33, 73, 6, 1, 24]]) {
    if (temps[key] == null) continue;
    const c = P(x, y, z, L), R = rad * Math.max(scaleNow, 1.2), rgb = heatRgb(temps[key]), gr = g.createRadialGradient(c[0], c[1], 0, c[0], c[1], R);
    gr.addColorStop(0, rgbCss(rgb, 0.5)); gr.addColorStop(1, rgbCss(rgb, 0));
    g.fillStyle = gr; g.beginPath(); g.arc(c[0], c[1], R, 0, Math.PI * 2); g.fill();
  }
  // 設計風向：固定的箭頭，和目前風量無關（沒有轉速也沒有風量讀值）
  const arrow = (a, b) => {
    g.strokeStyle = g.fillStyle = cInfo; g.lineWidth = 1.6; g.setLineDash([5, 4]); g.beginPath(); g.moveTo(a[0], a[1]); g.lineTo(b[0], b[1]); g.stroke(); g.setLineDash([]);
    const ang = Math.atan2(b[1] - a[1], b[0] - a[0]); g.beginPath(); g.moveTo(b[0], b[1]); g.lineTo(b[0] - 9 * Math.cos(ang - 0.4), b[1] - 9 * Math.sin(ang - 0.4)); g.lineTo(b[0] - 9 * Math.cos(ang + 0.4), b[1] - 9 * Math.sin(ang + 0.4)); g.closePath(); g.fill();
  };
  arrow(P(75, 75, -34, 0), P(75, 75, -6, 0));
  arrow(P(75, 148, 26, 3), P(75, 186, 26, 3));

  // 引線標註：依投影後的位置分到左右兩欄，欄內由上到下排開
  const notes = [
    {at: P(75, 60, 14, 2), l1: t('thermo.l_soc'), l2: 'TSOC ' + fmtC(D.soc), rgb: heatRgb(D.soc)},
    {at: P(33, 73, 6, 1), l1: t('thermo.l_ssd'), l2: fmtC(D.nvme), rgb: heatRgb(D.nvme)},
    {at: P(110, 70, 34, 4), l1: t('thermo.l_fans'), l2: t('thermo.fan_rpm'), l3: thermoStill() ? '' : t('thermo.fan_anim')},
    {at: P(75, 133, 38, 3), l1: t('thermo.l_fins'), l2: t('thermo.l_exhaust')},
    {at: P(75, 75, -30, 0), l1: t('thermo.l_intake'), l2: t('thermo.design_flow')},
    {at: P(75, 0, 30, 0), l1: t('thermo.l_front'), l2: t('thermo.surface')},
  ];
  const sides = {left: notes.filter(n => n.at[0] < W / 2), right: notes.filter(n => n.at[0] >= W / 2)}, taken = [];
  for (const side of ['left', 'right']) {
    const list = sides[side].sort((a, b) => a.at[1] - b.at[1]), gap = 40;
    let y = 22;
    for (const n of list) { n.y = Math.max(y, Math.min(n.at[1], H - 20 - gap * (list.length - 1 - list.indexOf(n)))); y = n.y + gap + (n.l3 ? 15 : 0); }
    for (const n of list) {
      g.font = font(12); const w = Math.max(g.measureText(n.l1).width, g.measureText(n.l2).width, n.l3 ? g.measureText(n.l3).width : 0), tx = side === 'left' ? 10 : W - 10, ex2 = side === 'left' ? tx + w + 8 : tx - w - 8;
      g.strokeStyle = cMuted; g.lineWidth = 0.8; g.beginPath(); g.moveTo(ex2, n.y); g.lineTo(ex2 + (side === 'left' ? 14 : -14), n.y); g.lineTo(n.at[0], n.at[1]); g.stroke();
      g.fillStyle = n.rgb ? rgbCss(n.rgb) : cMuted; g.beginPath(); g.arc(n.at[0], n.at[1], 3, 0, Math.PI * 2); g.fill();
      taken.push([side === 'left' ? tx : tx - w, n.y - 16, w, n.l3 ? 47 : 32]);
      g.textAlign = side; g.textBaseline = 'alphabetic'; g.fillStyle = cText; g.fillText(n.l1, tx, n.y - 3);
      g.fillStyle = n.rgb ? rgbCss(n.rgb) : cMuted; g.fillText(n.l2, tx, n.y + 12);
      if (n.l3) g.fillText(n.l3, tx, n.y + 27);
    }
  }
  // 方位字最後畫，撞到引線標註的字就往下讓。「後」跟著排氣箭頭走：放在地面會被鰭片擋住
  g.font = font(12); g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillStyle = cMuted;
  for (const [txt, at] of [[t('thermo.front'), P(75, -16, 0, 0)], [t('thermo.rear'), P(75, 200, 26, 3)]]) {
    const w = g.measureText(txt).width; let y = at[1];
    for (let i = 0; i < 6 && taken.some(r => at[0] + w / 2 > r[0] - 4 && at[0] - w / 2 < r[0] + r[2] + 4 && y + 8 > r[1] && y - 8 < r[1] + r[3]); i++) y += 14;
    g.fillText(txt, at[0], y);
  }
}

/* 晶片內的溫度區。只認後端給的對應：zone.cores 有值才進二乘二格，其餘各自成一格；名字的意義沒驗過的標「推測」。 */
function thermoTile(z, label, sub, badge, hint, cls) {
  return `<div class="tz ${cls || ''}" style="--h:${rgbCss(heatRgb(z.temp_c))}" title="${esc(hint || '')}"><span class="n mono">${esc(z.name || z.type || '?')}</span><span class="c">${label}</span><b class="mono">${fmtC(z.temp_c)}</b><span class="e">${sub || ''}${badge ? `<i>${badge}</i>` : ''}</span></div>`;
}
function thermoScale(label, temp, marks) {
  const lo = 30, hi = 110, pos = v => Math.max(0, Math.min(100, (v - lo) / (hi - lo) * 100)).toFixed(1) + '%';
  return `<div class="tsc"><span class="l">${label}</span><b class="mono">${fmtC(temp)}</b><div class="bar">${marks.map(m => `<i class="k" style="left:${pos(m[0])}" title="${esc(m[1])}"><u>${m[0]}</u></i>`).join('')}${temp == null ? '' : `<i class="m" style="left:${pos(temp)};background:${rgbCss(heatRgb(temp))}"></i>`}</div></div>`;
}
function thermoChipHtml(D) {
  const zones = D.zones, cored = zones.filter(z => z.cores), find = n => zones.find(z => z.name === n);
  if (!zones.length) return `<div class="empty">${t('thermo.no_zones')}</div>`;
  const coreTxt = c => c.first === c.last ? `cpu${c.first}` : `cpu${c.first}–${c.last}`;
  const evid = c => D.coresMeasured ? [t('thermo.measured'), t('thermo.measured_hint', {v0: coreTxt(c)})] : [t('thermo.by_name'), t('thermo.by_name_hint', {v0: coreTxt(c)})];
  const out = [`<div class="tz-head"><b>${t('thermo.chip_head')}</b><span class="sub1">${t('thermo.chip_note')}</span></div>`];
  const soc = find('TSOC');
  if (soc) out.push(thermoTile(soc, 'SoC', '', t('thermo.by_name'), t('thermo.soc_hint'), 'wide'));
  if (cored.length) {
    const kinds = ['E', 'P'].filter(k => cored.some(z => z.cores.kind === k)), rows = [...new Set(cored.map(z => z.name.slice(0, 3)))];
    const head = k => { const c = cored.find(z => z.cores.kind === k).cores; return `${k}${c.part ? ' · ' + esc(c.part) : ''}`; };
    out.push(`<div class="tz-grid" style="grid-template-columns:auto repeat(${kinds.length},1fr)"><span></span>${kinds.map(k => `<span class="h">${head(k)}</span>`).join('')}` +
      rows.map(r => `<span class="r mono">${esc(r)}</span>` + kinds.map(k => { const z = cored.find(q => q.name.slice(0, 3) === r && q.cores.kind === k); if (!z) return '<span></span>'; const [b, h] = evid(z.cores); return thermoTile(z, coreTxt(z.cores), '', b, h); }).join('')).join('') + `</div>`);
  }
  const rest = zones.filter(z => !z.cores && z.name !== 'TSOC' && z.name !== 'TGPU');
  const tgpu = find('TGPU'), marksAcpi = tgpu && tgpu.crit_c != null ? [[tgpu.crit_c, t('thermo.acpi_crit', {v0: tgpu.crit_c})]] : [];
  const marksNvml = D.nvmlThresholds ? [[D.slowdown, t('thermo.nvml_slow', {v0: D.slowdown})]].concat(D.shutdown ? [[D.shutdown, t('thermo.nvml_shut', {v0: D.shutdown})]] : []) : [];
  if (tgpu || D.hasGpu) out.push(`<div class="tz-gpu" title="${esc(t('thermo.gpu_two_hint'))}"><div class="tz-head"><b>GPU</b><span class="sub1">${t('thermo.gpu_two')}</span></div>${tgpu ? thermoScale('ACPI TGPU', tgpu.temp_c, marksAcpi) : ''}${D.hasGpu ? thermoScale('NVML', D.gpuNvml, marksNvml) : ''}</div>`);
  const extra = rest.map(z => z.name === 'TUNC' ? thermoTile(z, 'uncore', '', t('thermo.inferred'), t('thermo.uncore_hint')) : thermoTile(z, '', '', '', ''));
  if (D.hasNvme) extra.push(thermoTile({name: 'NVMe', temp_c: D.nvme}, t('thermo.nvme'), '', '', t('thermo.nvme_hint')));
  if (D.hasWifi) extra.push(thermoTile({name: 'Wi-Fi', temp_c: D.wifi}, '', t('thermo.wifi_pos'), '', t('thermo.wifi_hint', {v0: D.wifiChip}), 'wide'));
  if (extra.length) out.push(`<div class="tz-row">${extra.join('')}</div>`);
  return out.join('');
}

/* 風扇動畫：面板在畫面上才跑，每秒約 30 格；系統設定「減少動態效果」時不轉。 */
const THERMO_SPIN = 2.2, THERMO_FRAME = 33;   // 弧度／秒、毫秒
const thermoStill = () => !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches);
function thermoTick(ts) {
  thermo.raf = 0;
  if (!thermo.seen || thermoStill()) return;
  if (ts - thermo.last >= THERMO_FRAME) { thermo.spin = ts / 1000 * THERMO_SPIN % (Math.PI * 2); thermo.last = ts; thermoDraw(); }
  thermo.raf = requestAnimationFrame(thermoTick);
}
function thermoSpinSync() { if (thermo.seen && !thermo.raf && !thermoStill()) thermo.raf = requestAnimationFrame(thermoTick); }

function thermoBind() {
  if (thermo.bound) return;
  thermo.bound = true;
  const cv = $('#thermoCanvas'), slider = $('#thermoExplode');
  const save = () => { try { localStorage.setItem(THERMO_KEY, JSON.stringify({yaw: thermo.yaw, elev: thermo.elev, explode: thermo.explode})); } catch (_) {} };
  slider.value = Math.round(thermo.explode * 100);
  slider.oninput = () => { thermo.explode = slider.value / 100; thermoDraw(); save(); };
  $('#thermoReset').onclick = () => { Object.assign(thermo, THERMO_VIEW); slider.value = 0; thermoDraw(); save(); };
  cv.addEventListener('pointerdown', e => { thermo.drag = [e.clientX, e.clientY]; cv.setPointerCapture(e.pointerId); cv.classList.add('grabbing'); });
  cv.addEventListener('pointermove', e => {
    if (!thermo.drag) return;
    thermo.yaw += (e.clientX - thermo.drag[0]) * 0.01;
    thermo.elev = Math.max(-1.35, Math.min(1.5, thermo.elev + (e.clientY - thermo.drag[1]) * 0.01));
    thermo.drag = [e.clientX, e.clientY]; thermoDraw();
  });
  const end = () => { if (thermo.drag) { thermo.drag = null; cv.classList.remove('grabbing'); save(); } };
  cv.addEventListener('pointerup', end); cv.addEventListener('pointercancel', end);
  window.addEventListener('resize', thermoDraw);
  if (window.IntersectionObserver) new IntersectionObserver(es => { thermo.seen = es[es.length - 1].isIntersecting; thermoSpinSync(); }).observe(cv);
  if (window.matchMedia) matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change', thermoSpinSync);
  $('#thermoLegend').innerHTML = `<span class="sub1">${t('thermo.scale')}</span><span class="grad" style="background:linear-gradient(90deg,${THERMO_STOPS.map(s => `${rgbCss(s[1])} ${((s[0] - 35) / 65 * 100).toFixed(0)}%`).join(',')})">${THERMO_STOPS.map(s => `<u style="left:${((s[0] - 35) / 65 * 100).toFixed(0)}%">${s[0]}</u>`).join('')}</span><span class="sub1">°C</span><span class="nd"><i style="background:${rgbCss(THERMO_NODATA)}"></i>${t('thermo.no_reading')}</span>`;
}

function renderThermal(j, gpuStatic) {
  const panel = $('#thermoPanel'); if (!panel) return;
  const T = j.thermal;
  if (!T) { panel.classList.add('hide'); return; }
  panel.classList.remove('hide');
  const sens = j.sensors || [], nv = sens.find(s => s.chip === 'nvme' && /composite/i.test(s.label)), wf = sens.find(s => /^(mt|iwl|ath|rtw|brcm)/i.test(s.chip));
  const soc = T.zones.find(z => z.name === 'TSOC'), G = gpuStatic || {};
  thermo.data = {
    zones: T.zones, coresMeasured: !!T.cores_measured, soc: soc ? soc.temp_c : null,
    hasNvme: !!nv, nvme: nv ? nv.temp_c : null, hasWifi: !!wf, wifi: wf ? wf.temp_c : null, wifiChip: wf ? wf.chip : '',
    hasGpu: !!j.gpu, gpuNvml: j.gpu && j.gpu.temp_c != null ? j.gpu.temp_c : null,
    nvmlThresholds: G.throttle_source === 'NVML slowdown threshold' && G.throttle_temp_c != null, slowdown: G.throttle_temp_c, shutdown: G.shutdown_temp_c,
  };
  thermoBind();
  $('#thermoBody').classList.toggle('hide', !T.chassis);
  const none = $('#thermoNoChassis'); none.classList.toggle('hide', !!T.chassis);
  if (!T.chassis) none.textContent = t('thermo.no_chassis', {v0: T.product || '—'});
  $('#thermoChip').innerHTML = thermoChipHtml(thermo.data);
  thermoDraw();
}
