/* AI 布告欄：新模型表（OpenRouter 公開清單）＋這台機器上的 AI 工具落後幾版、新版加了哪些模型＋VS Code 擴充新版。
   後端 /api/ai-board 不等網路：先回快取，過期就背景重抓（約 20 秒），refreshing 為真時這裡每 3 秒再問。
   「新」：比這個瀏覽器上次看布告欄時最新的那個模型還新（localStorage，只是看過沒看過的方便標記）。 */
const AI_PROVIDERS = ['anthropic', 'openai', 'google', 'deepseek', 'qwen', 'moonshotai', 'z-ai', 'minimax', 'x-ai', 'meta', 'mistralai', 'nvidia', 'xiaomi', 'tencent', 'baidu'];
const aiProvName = p => { p = p === 'meta-llama' ? 'meta' : p; return AI_PROVIDERS.includes(p) ? t('ai.prov_' + p.replace(/-/g, '_')) : p; };
const aiState = {prov: 'major', timer: null, seenPrev: null, all: false};
const AI_ROWS = 20;   // 主要供應商近 90 天就有五、六十個，全列會把下面的工具表推出畫面
const AI_SEEN_KEY = 'spark-center.aiSeen';
const aiSeenGet = () => { try { return Number(localStorage.getItem(AI_SEEN_KEY)) || 0; } catch (e) { return 0; } };
const aiSeenSet = v => { try { localStorage.setItem(AI_SEEN_KEY, String(v)); } catch (e) {} };
const aiDate = s => s ? new Date(s * 1000).toLocaleDateString(LANG === 'en' ? 'en-CA' : 'zh-TW', {month: '2-digit', day: '2-digit'}) : '—';
const aiPrice = v => v == null ? '—' : v === 0 ? t('ai.free') : '$' + Number(v.toPrecision(3));   // 每百萬 token 美元
const aiIsToday = s => s && new Date(s * 1000).toDateString() === new Date().toDateString();   // 這台電腦的本地日期
const aiCtx = n => !n ? '—' : n >= 1e6 ? Math.round(n / 1e5) / 10 + 'M' : Math.round(n / 1000) + 'K';

async function loadAi(force) {
  clearTimeout(aiState.timer);
  let r;
  try { r = await api('/api/ai-board' + (force ? '?force=1' : '')); } catch (e) { r = {ok: false, error: String(e)}; }
  if (!r.ok) { $('#aiModels').innerHTML = `<div class="panel notice danger">${esc(r.error || '—')}</div>`; return; }
  if (aiState.seenPrev == null) aiState.seenPrev = aiSeenGet();   // 這次打開分頁時的「上次看到哪」；整段瀏覽期間沿用，「新」不會一重畫就消失
  renderAi(r.data, r.refreshing);
  if (r.refreshing) aiState.timer = setTimeout(() => { if (!$('#tab-ai').classList.contains('hide')) loadAi(); }, 3000);
}

function renderAi(d, refreshing) {
  const busy = refreshing ? ` · ${t('ai.refreshing')}` : '';
  if (!d) { $('#aiModels').innerHTML = `<div class="empty">${t('ai.first_fetch')}</div>`; $('#aiModelsMeta').textContent = ''; return; }
  renderAiModels(d.models, busy, d.models.fetched || d.generated);
  renderAiTools(d.tools || [], d.generated, busy);
  renderAiVscode(d.vscode);
  const top = Math.max(0, ...(d.models.items || []).filter(m => m.major).map(m => m.created));
  if (top) aiSeenSet(Math.max(top, aiSeenGet()));
  $('#dotAi').classList.add('hide');
}

function renderAiModels(m, busy, generated) {
  const meta = $('#aiModelsMeta');
  if (!m.ok) {
    meta.textContent = '';
    $('#aiModels').innerHTML = `<div class="panel notice warn">${t('ai.models_failed', {error: esc(m.error || '—')})}</div>`;
    return;
  }
  const items = m.items;
  const counts = {}; items.forEach(x => { const k = x.major ? (x.provider === 'meta-llama' ? 'meta' : x.provider) : 'other'; counts[k] = (counts[k] || 0) + 1; });
  const chips = [['major', t('ai.chip_major'), items.filter(x => x.major).length], ['all', t('common.all'), items.length],
    ...AI_PROVIDERS.filter(p => counts[p]).map(p => [p, aiProvName(p), counts[p]]), ...(counts.other ? [['other', t('ai.prov_other'), counts.other]] : [])];
  $('#aiProvChips').innerHTML = chips.map(([k, label, n]) => `<span class="chip${aiState.prov === k ? ' active' : ''}" data-prov="${k}">${esc(label)} <span class="sub1">${n}</span></span>`).join('');
  $('#aiProvChips').querySelectorAll('.chip').forEach(c => c.onclick = () => { aiState.prov = c.dataset.prov; aiState.all = false; renderAiModels(m, busy, generated); });
  const openOnly = $('#aiOpenOnly').checked;
  const rows = items.filter(x => (aiState.prov === 'all' || (aiState.prov === 'major' ? x.major : aiState.prov === 'other' ? !x.major : (x.provider === aiState.prov || (aiState.prov === 'meta' && x.provider === 'meta-llama')))) && (!openOnly || x.hf));
  meta.textContent = t('ai.models_meta', {n: items.length, days: m.days, at: (generated || '—').replace('T', ' ')}) + busy;
  const seen = aiState.seenPrev || 0;
  $('#aiModels').innerHTML = !rows.length ? `<div class="empty">${t('ai.no_models')}</div>` :
    `<div style="overflow-x:auto"><table><thead><tr><th style="width:70px">${t('ai.col_date')}</th><th>${t('ai.col_model')}</th><th style="width:130px">${t('ai.col_provider')}</th><th style="width:70px">${t('ai.col_context')}</th><th style="width:120px" title="${esc(t('ai.price_hint'))}">${t('ai.col_price')}</th><th style="width:120px">${t('ai.col_open')}</th></tr></thead><tbody>` +
    rows.slice(0, aiState.all ? rows.length : AI_ROWS).map(x => `<tr><td class="mono sub1">${aiDate(x.created)}</td>
      <td><b title="${esc(x.desc)}">${esc(x.name)}</b>${aiIsToday(x.created) ? ` <span class="tag new">${t('ai.today')}</span>` : seen && x.created > seen ? ` <span class="tag new">${t('ai.new')}</span>` : ''}<div class="sub1 mono">${esc(x.id)}</div></td>
      <td class="sub1">${esc(aiProvName(x.provider))}</td>
      <td class="mono sub1">${aiCtx(x.context)}</td>
      <td class="mono sub1">${aiPrice(x.price_in)} / ${aiPrice(x.price_out)}</td>
      <td>${x.hf ? `<a class="tag ok" href="https://huggingface.co/${esc(x.hf)}" target="_blank" rel="noopener" title="${esc(x.hf)}">${t('ai.open_weights')}</a>` : '<span class="sub1">—</span>'}</td></tr>`).join('') + '</tbody></table></div>'
    + (rows.length > AI_ROWS ? `<div style="margin-top:8px"><button class="small" id="aiMore">${aiState.all ? t('ai.show_less') : t('ai.show_all', {n: rows.length})}</button></div>` : '');
  const more = $('#aiMore'); if (more) more.onclick = () => { aiState.all = !aiState.all; renderAiModels(m, busy, generated); };
  $('#aiModelsNote').textContent = t('ai.models_note');
}

function aiModelTags(tool) {
  if (tool.notes_err) return `<span class="sub1" title="${esc(tool.notes_err)}">—</span>`;
  const ms = (tool.notes && tool.notes.models) || [];
  if (!ms.length) return tool.update ? `<span class="sub1">${t('ai.no_model_news', {n: tool.notes ? tool.notes.versions : 0})}</span>` : '';
  return ms.map(m => `<span class="tag new" title="${esc(m.line)}">${esc(m.mentions[0])} · ${esc(m.version)}</span>`).join(' ');
}

function renderAiTools(tools, generated, busy) {
  $('#aiToolsMeta').textContent = t('ai.tools_meta', {at: (generated || '—').replace('T', ' ')}) + busy;
  if (!tools.length) { $('#aiTools').innerHTML = `<div class="empty">${t('ai.no_tools')}</div>`; return; }
  const ro = document.body.classList.contains('ro');
  const row = tool => {
    const latest = tool.latest ? esc(tool.latest) : `<span title="${esc(tool.latest_err || '')}">—</span>`;
    const status = tool.update ? `<span class="to mono">→ ${latest}</span>` : tool.latest ? `<span class="tag ok">${t('updates.up_to_date')}</span>` : latest;
    let act = '';
    if (tool.key === 'claude_vscode' && tool.update) act = `<button class="small primary" data-ext="${esc(tool.ext)}"${ro ? ' disabled' : ''}>${t('common.update')}</button><div class="sub1">${t('ai.reload_vscode')}</div>`;
    else if (tool.key === 'claude_cli' && tool.update) act = `<a class="sub1" href="#updates">${t('ai.cli_in_npm')}</a>`;
    else if (tool.key === 'codex' && tool.update) act = tool.host_update ? `<button class="small primary" data-ext="${esc(tool.host_ext)}"${ro ? ' disabled' : ''}>${t('ai.update_host')}</button>` : `<span class="sub1">${t('ai.codex_wait_host')}</span>`;
    return `<tr><td><b>${t('ai.tool_' + tool.key)}</b><div class="sub1">${t('ai.tool_' + tool.key + '_sub')}</div></td>
      <td class="mono">${esc(tool.installed || '—')}</td><td>${status}</td><td>${aiModelTags(tool)}</td><td>${act}</td></tr>`;
  };
  $('#aiTools').innerHTML = `<div style="overflow-x:auto"><table><thead><tr><th style="width:26%">${t('ai.col_tool')}</th><th style="width:15%">${t('ai.col_installed')}</th><th style="width:12%">${t('ai.col_latest')}</th><th>${t('ai.col_new_models')}</th><th style="width:170px"></th></tr></thead><tbody>${tools.map(row).join('')}</tbody></table></div>`;
  aiBindUpdate($('#aiTools'));
}

function renderAiVscode(v) {
  if (!v || !v.readable) { $('#aiVscodeMeta').textContent = ''; $('#aiVscode').innerHTML = `<div class="empty">${t('ai.vscode_none')}</div>`; return; }
  const items = v.items, upd = items.filter(e => e.update);
  $('#aiVscodeMeta').textContent = v.ok ? t('ai.vscode_meta', {n: items.length, u: upd.length}) : t('ai.vscode_meta_err', {n: items.length, error: v.error || '—'});
  const ro = document.body.classList.contains('ro');
  const allBtn = upd.length > 1 ? `<button class="small primary" id="aiUpdAll" data-exts="${esc(upd.map(e => e.id).join(' '))}"${ro ? ' disabled' : ''}>${t('ai.update_all', {n: upd.length})}</button>` : '';
  const tr = e => `<tr><td><b>${esc(e.name)}</b><div class="sub1 mono">${esc(e.id)}${e.pre ? ` <span class="tag">${t('ai.pre')}</span>` : ''}</div></td><td class="mono">${esc(e.version)}</td>
    <td>${e.update ? `<span class="to mono">→ ${esc(e.latest)}</span>` : e.latest ? `<span class="tag ok">${t('updates.up_to_date')}</span>` : '<span class="sub1">—</span>'}</td>
    <td>${e.update ? `<button class="small primary" data-ext="${esc(e.id)}"${ro ? ' disabled' : ''}>${t('common.update')}</button>` : ''}</td></tr>`;
  const head = `<thead><tr><th>${t('ai.col_ext')}</th><th style="width:18%">${t('ai.col_installed')}</th><th style="width:18%">${t('ai.col_latest')}</th><th style="width:100px"></th></tr></thead>`;
  const rest = items.filter(e => !e.update);
  $('#aiVscode').innerHTML = (allBtn ? `<div style="margin-bottom:8px">${allBtn}</div>` : '') + (upd.length ? `<div style="overflow-x:auto"><table>${head}<tbody>${upd.map(tr).join('')}</tbody></table></div><div class="sub1" style="margin-top:6px">${t('ai.reload_vscode')}</div>` : `<div class="sub1">${t('ai.vscode_all_current')}</div>`)
    + (rest.length ? `<details style="margin-top:8px"><summary class="sub1" style="cursor:pointer">${t('ai.vscode_rest', {n: rest.length})}</summary><div style="overflow-x:auto"><table>${head}<tbody>${rest.map(tr).join('')}</tbody></table></div></details>` : '');
  aiBindUpdate($('#aiVscode'));
}

function aiBindUpdate(box) {
  box.querySelectorAll('button[data-ext], button[data-exts]').forEach(b => b.onclick = async () => {
    const ids = b.dataset.exts ? b.dataset.exts.split(' ') : [b.dataset.ext];
    if (ids.length > 1 && !confirm(t('ai.update_all_confirm', {n: ids.length, list: ids.join('\n')}))) return;
    box.querySelectorAll('button[data-ext], button[data-exts]').forEach(x => x.disabled = true);
    const r = await api('/api/vscode/update', {ids});
    if (!r.ok) { alert(t('updates.could_not_start_with_value', {value: r.error})); box.querySelectorAll('button[data-ext], button[data-exts]').forEach(x => x.disabled = false); return; }
    startPolling(t('ai.updating_ext', {id: ids.length > 1 ? t('ai.n_exts', {n: ids.length}) : ids[0]}));
    window.scrollTo({top: 0, behavior: 'smooth'});
    const w = setInterval(async () => {   // 工作結束後重抓布告欄（後端在工作結束時把快取作廢）
      const j = await api('/api/job');
      if (j.status !== 'running') { clearInterval(w); if (!$('#tab-ai').classList.contains('hide')) loadAi(); }
    }, 3000);
  });
}

async function aiDotCheck() {
  let r; try { r = await api('/api/ai-board'); } catch (e) { return; }
  if (!r.ok || !r.data || !r.data.models.ok) return;
  if (!$('#tab-ai').classList.contains('hide')) return;   // 正在看布告欄就不亮
  const seen = aiSeenGet();
  // 亮點：今天（本地日期）有主要供應商的模型上架，而且這個瀏覽器還沒在布告欄看過它；看過就熄，隔天有新的再亮
  $('#dotAi').classList.toggle('hide', !r.data.models.items.some(m => m.major && aiIsToday(m.created) && m.created > seen));
}
setInterval(aiDotCheck, 30 * 60 * 1000);   // App 常整天開著：後端每小時重抓模型清單，這裡半小時看一次
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') aiDotCheck(); });

$('#btnAiReload').onclick = () => loadAi(true);
$('#aiOpenOnly').onchange = () => loadAi();
