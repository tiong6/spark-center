// 量各分頁「可見說明文字」字數（.sub1/.sub/p/.muted/.empty，不含表格格、FAQ 內文、視窗）。用法：HC=<空目錄> node tools/measure_prose.mjs（服務要在 11001 跑）
import { spawn } from 'node:child_process';
const port = 9349;
const chrome = spawn('google-chrome', ['--headless=new', '--disable-gpu', '--no-sandbox', `--user-data-dir=${process.env.HC}`, `--remote-debugging-port=${port}`, '--window-size=1400,1000', 'about:blank'], { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms)); let ws;
try {
  let targets = [];
  for (let i = 0; i < 40 && !targets.length; i++) { await sleep(250); try { targets = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).filter(t => t.type === 'page'); } catch {} }
  ws = new WebSocket(targets[0].webSocketDebuggerUrl); await new Promise(r => ws.onopen = r);
  let id = 0; const pending = new Map();
  ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
  const send = (method, params = {}) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async expr => (await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true })).result?.result?.value;
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.navigate', { url: 'http://127.0.0.1:11001/#updates' }); await sleep(1500);
  await ev(`localStorage.setItem('spark-center.lang', 'zh-TW')`); await send('Page.reload'); await sleep(4000);
  for (const tab of ['monitor','llm','updates','apps','disk','hardware','history','faq','setup']) {
    await ev(`showTab('${tab}')`); await sleep(tab === 'hardware' || tab === 'apps' ? 6000 : 3000);
    const r = await ev(`(() => { const root = document.querySelector('#tab-${tab}'); const all = root.innerText.replace(/\\s+/g,'').length;
      // 說明性文字：.sub1 / .sub / p / .muted / details>summary 這些「解釋用」元素，不含表格儲存格裡的
      const prose = [...root.querySelectorAll('.sub1, .sub, p, .muted, .empty')].filter(e => !e.closest('td') && !e.closest('.faq') && !e.closest('.modal'));
      const n = prose.reduce((s, e) => s + e.innerText.replace(/\\s+/g,'').length, 0);
      const top = prose.map(e => e.innerText.replace(/\\s+/g,' ').trim()).filter(t => t.length > 60).sort((a,b) => b.length - a.length).slice(0, 3).map(t => t.length + ': ' + t.slice(0, 70) + '…');
      return JSON.stringify({all, prose: n, panels: root.querySelectorAll('.panel').length, top}); })()`);
    const j = JSON.parse(r); console.log(tab.padEnd(9), 'all', String(j.all).padStart(6), 'prose', String(j.prose).padStart(6), (j.prose / Math.max(j.all,1) * 100).toFixed(0).padStart(3) + '%', 'panels', j.panels); for (const t of j.top) console.log('    ', t);
  }
} finally { try { ws && ws.close(); } catch {} chrome.kill(); }
