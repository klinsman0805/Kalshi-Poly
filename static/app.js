// ── State ─────────────────────────────────────────────────────────────────────
const S = {
  tab: 'scalp', botRunning: false,
  scalp: {}, scalpSession: {},
  copy: [], copyCfg: {}, copyEnabled: false, copyScanning: false, copyExec: null,
  lastDataTs: 0, logExpanded: false,
};

// ── Tabs ──────────────────────────────────────────────────────────────────────
const TABS = ['scalp', 'copy'];
function showTab(t) {
  S.tab = t;
  TABS.forEach(x => {
    document.getElementById('tab-' + x).classList.toggle('active', t === x);
    document.getElementById('view-' + x).classList.toggle('active', t === x);
  });
  if (t === 'copy') renderCopy();
}

// ── SSE ───────────────────────────────────────────────────────────────────────
let es = null, _sseOn = true;
function connectSSE() {
  _sseOn = true;
  if (es) try { es.close(); } catch (e) {}
  es = new EventSource('/stream');
  es.onopen = () => setConn(true);
  es.onerror = () => { setConn(false); if (_sseOn) setTimeout(connectSSE, 3000); };
  es.onmessage = e => handleMsg(JSON.parse(e.data));
}
function setConn(ok) {
  document.getElementById('conn-dot').classList.toggle('live', ok);
  document.getElementById('conn-label').textContent = ok ? 'Connected' : 'Reconnecting…';
}

function handleMsg(m) {
  switch (m.type) {
    case 'init':     updateStatus(m.status); break;
    case 'status':   updateStatus(m.status); break;
    case 'log':      addLog(m); break;
    case 'scalping':
      S.scalp = m.assets || {}; S.scalpSession = m.session || {};
      S.lastDataTs = Date.now(); if (S.tab === 'scalp') renderScalp();
      renderScalpSummary(); break;
    case 'copytrade':
      applyCopy(m); break;
  }
}

// ── Status ────────────────────────────────────────────────────────────────────
function updateStatus(st) {
  const dot = document.getElementById('sdot');
  dot.className = 'sdot';
  S.botRunning = ['monitoring', 'connected'].includes(st);
  if (S.botRunning) dot.classList.add('run');
  else if (['discovering', 'connecting', 'starting', 'reconnecting', 'waiting'].includes(st))
    dot.classList.add('disc');
  document.getElementById('slabel').textContent = (st || 'stopped').toUpperCase();
  document.getElementById('btn-start').style.display = S.botRunning ? 'none' : '';
  document.getElementById('btn-stop').style.display = S.botRunning ? '' : 'none';
}

// ── Scalping render ───────────────────────────────────────────────────────────
const c = v => (v == null ? '—' : v + '¢');
function fmtSpot(v) { return v == null ? '—' : '$' + v.toLocaleString(undefined, {maximumFractionDigits: 2}); }
function fmtSecs(s) { if (s == null) return '—'; return s >= 60 ? Math.floor(s/60)+'m '+(s%60)+'s' : s+'s'; }

function sigClass(sig) {
  if (!sig) return 'flat';
  if (sig.startsWith('ENTER')) return 'enter';
  if (sig === 'FEE-BLOCKED') return 'blocked';
  if (sig === 'SETTLING') return 'settling';
  return 'flat';
}

function renderScalp() {
  const grid = document.getElementById('scalp-grid');
  const assets = ['BTC', 'ETH', 'SOL'].filter(a => S.scalp[a]);
  if (!assets.length) { grid.innerHTML = '<div class="no-data">Waiting for market data…</div>'; return; }
  grid.innerHTML = assets.map(a => renderScard(S.scalp[a])).join('');
}

function renderScard(v) {
  const sc = sigClass(v.signal);
  const timerCls = v.secs_left == null ? '' : v.secs_left < 60 ? 'crit' : v.secs_left < 180 ? 'warn' : '';
  const vel = v.vel30;
  const velCls = vel == null ? 'flat' : vel > 0 ? 'up' : vel < 0 ? 'dn' : 'flat';
  const velStr = vel == null ? '' : (vel > 0 ? '▲ +' : '▼ ') + Math.abs(vel).toFixed(2) + '/30s';
  const mktPct = v.mkt_prob != null ? (v.mkt_prob * 100) : null;
  const mdlPct = v.model_prob != null ? (v.model_prob * 100) : null;
  const edgeCls = v.net_edge_c == null ? '' : v.net_edge_c > 0 ? 'ok' : 'dn';
  const cardCls = 'scard ' + (sc === 'enter' ? 'enter' : sc === 'blocked' ? 'blocked' : '');
  const paper = v.paper
    ? `<span class="paper">PAPER ${v.paper.side} @ ${v.paper.entry_cost}¢</span>` : '';
  return `<div class="${cardCls}">
    <div class="scard-head">
      <span class="scard-asset">${v.asset}</span>
      <span class="sig ${sc}">${v.signal || '—'}</span>
      <span class="scard-timer ${timerCls}">${fmtSecs(v.secs_left)}</span>
    </div>
    <div class="spot-row">
      <span class="spot-val">${fmtSpot(v.spot)}</span>
      <span class="vel ${velCls}">${velStr}</span>
    </div>
    <div class="probbar">
      ${mktPct != null ? `<div class="mkt" style="width:${mktPct}%"></div>` : ''}
      ${mdlPct != null ? `<div class="mdl" style="left:${mdlPct}%"></div>` : ''}
      <span class="lbl">mkt ${mktPct != null ? mktPct.toFixed(0) : '–'}%</span>
      <span class="lbl r">model ${mdlPct != null ? mdlPct.toFixed(0) : '–'}%</span>
    </div>
    <div class="kv-grid">
      <div class="kv"><div class="k">Kalshi YES</div><div class="v up">${c(v.yes_bid)} / ${c(v.yes_ask)}</div></div>
      <div class="kv"><div class="k">Kalshi NO</div><div class="v dn">${c(v.no_bid)} / ${c(v.no_ask)}</div></div>
      <div class="kv"><div class="k">Strike</div><div class="v">${v.strike != null ? fmtSpot(v.strike) : '—'}</div></div>
      <div class="kv"><div class="k">Net edge (after fee)</div><div class="v ${edgeCls}">${v.net_edge_c != null ? (v.net_edge_c>0?'+':'')+v.net_edge_c+'¢' : '—'}</div></div>
    </div>
    <div class="scard-foot">
      <span>gross ${v.gross_edge_c != null ? v.gross_edge_c+'¢' : '—'}</span>
      <span>fee ${v.fee_c != null ? v.fee_c+'¢' : '—'} ×2</span>
      <span>vol ${v.vol != null ? (v.vol*100).toFixed(0)+'%' : '—'}</span>
      <span>window ${v.window_pct != null ? v.window_pct+'%' : '—'}</span>
      ${paper}
    </div>
  </div>`;
}

function renderScalpSummary() {
  const s = S.scalpSession || {};
  const wr = s.trades ? Math.round(s.wins / s.trades * 100) : null;
  document.getElementById('scalp-summary').innerHTML =
    `Crypto scalping · Kalshi 15-min up/down vs live spot &nbsp;·&nbsp; ` +
    `paper P&L <b style="color:${(s.pnl||0)>=0?'var(--ok)':'var(--down)'}">${(s.pnl||0)>=0?'+':''}$${(s.pnl||0).toFixed(4)}</b> ` +
    `· ${s.trades||0} settled · ${wr!=null?wr+'% win':'—'}`;
}

// ── Copy-trade render ───────────────────────────────────────────────────────────
function applyCopy(m) {
  S.copyEnabled = !!m.enabled;
  S.copy = m.rows || [];
  S.copyCfg = m.config || {};
  S.copyExec = m.exec || null;
  // reveal the tab once the scanner is on
  document.getElementById('tab-copy').style.display = S.copyEnabled ? '' : 'none';
  // keep the metric/window selects in sync with backend config
  const ms = document.getElementById('copy-metric'), ws = document.getElementById('copy-window');
  if (S.copyCfg.metric && document.activeElement !== ms) ms.value = S.copyCfg.metric;
  if (S.copyCfg.window && document.activeElement !== ws) ws.value = S.copyCfg.window;
  S.lastDataTs = Date.now();
  renderCopySummary();
  renderCopyExec();
  if (S.tab === 'copy') renderCopy();
}

function renderCopyExec() {
  const e = S.copyExec; if (!e) return;
  const s = e.session || {};
  const mode = document.getElementById('copyexec-mode');
  if (e.live)          { mode.textContent = '🔴 LIVE forward-test — REAL orders'; mode.className = 'exec-arm live'; }
  else if (e.env_armed){ mode.textContent = '🟢 armed · paper forward-test'; mode.className = 'exec-arm ok'; }
  else                 { mode.textContent = '📄 PAPER forward-test'; mode.className = 'exec-arm'; }
  document.getElementById('cx-copied').textContent = s.copied || 0;
  document.getElementById('cx-open').textContent = (e.open || []).length;
  document.getElementById('cx-settled').textContent = s.settled || 0;
  document.getElementById('cx-win').textContent = s.win_rate == null ? '—' : Math.round(s.win_rate*100) + '%';
  const pnl = document.getElementById('cx-pnl');
  pnl.textContent = (s.realized_pnl >= 0 ? '+$' : '-$') + Math.abs(s.realized_pnl||0).toFixed(2);
  pnl.style.color = (s.realized_pnl||0) >= 0 ? 'var(--ok)' : 'var(--down)';
  document.getElementById('cx-slip').textContent = s.avg_slippage_c == null ? '—' : (s.avg_slippage_c>0?'+':'') + s.avg_slippage_c + '¢';
  document.getElementById('cx-follow').textContent = (e.follow || []).length;

  const box = document.getElementById('copyexec-pos');
  const open = e.open || [];
  if (!open.length) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="pos-hd">Open paper copies</div>' + open.map(p =>
    `<div class="pos-row ${p.mode}">
       <span class="pos-mode">${p.mode === 'live' ? 'LIVE' : 'PAPER'}</span>
       <span class="pos-match">${esc(p.title||'')}</span>
       <span class="pos-buy">${esc(p.outcome||'')} @ ${p.entry}¢ ×${p.filled}</span>
       <span class="pos-cost">$${p.cost_usd}</span>
       <span class="pos-chip">slip ${p.slippage_c>0?'+':''}${p.slippage_c}¢</span>
     </div>`).join('');
}

const money = v => v == null ? '—' : (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString(undefined, {maximumFractionDigits: 0});
const pctOrDash = v => v == null ? '—' : (v * 100).toFixed(0) + '%';

function renderCopy() {
  const body = document.getElementById('copy-body');
  const note = document.getElementById('copy-note');
  const scanBtn = document.getElementById('copy-scan');
  scanBtn.textContent = S.copyScanning ? '… scanning' : '⟳ Re-scan';
  if (!S.copyEnabled) {
    body.innerHTML = '<tr><td colspan="8"><div class="no-data">Copy-trade scanner is off — set COPYTRADE_ENABLED=true and restart.</div></td></tr>';
    note.textContent = '';
    return;
  }
  note.innerHTML = '⚠ Profit & Real Win% are the trustworthy signals · ROI inflates when history is capped (*) · winrate ≠ profitable';
  const onlyCopyable = document.getElementById('copy-only').checked;
  let rows = S.copy.slice();
  if (onlyCopyable) rows = rows.filter(r => r.copyable);
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="8"><div class="no-data">${S.copyScanning ? 'Scanning leaderboard…' : 'No traders match.'}</div></td></tr>`;
    return;
  }
  body.innerHTML = rows.map(r => {
    const cap = r.realized_capped ? '<span class="basis-tag" title="History capped at max_events — partial">*</span>' : '';
    const roiCls = r.realized_roi == null ? '' : r.realized_roi >= 0 ? 'up' : 'dn';
    const profCls = r.profit >= 0 ? 'up' : 'dn';
    const wr = r.realized_winrate;
    const wrCls = wr == null ? '' : wr >= 0.6 ? 'up' : wr < 0.45 ? 'dn' : '';
    return `<tr>
      <td class="l"><div class="match"><a href="${r.url}" target="_blank" rel="noopener">${esc(r.name)}</a></div>
        <div class="kickoff">${r.wallet.slice(0,8)}…${r.wallet.slice(-4)}</div></td>
      <td><span class="${profCls}">${money(r.profit)}</span></td>
      <td><span class="${wrCls}">${pctOrDash(wr)}</span>${cap}</td>
      <td><span class="${roiCls}">${r.realized_roi == null ? '—' : (r.realized_roi*100).toFixed(0)+'%'}</span></td>
      <td>${r.resolved_markets == null ? '—' : r.resolved_markets}</td>
      <td>${money(r.avg_trade_usd)}</td>
      <td>${r.open_positions}</td>
      <td>${r.copyable ? '<span class="vchip cross">✓ copyable</span>' : '<span class="vchip none">size</span>'}</td>
    </tr>`;
  }).join('');
}

function renderCopySummary() {
  const cfg = S.copyCfg || {};
  const copyable = S.copy.filter(r => r.copyable).length;
  const deep = cfg.deep ? `deep · lifetime winrate (≤${cfg.max_events} evts)` : 'shallow · snapshot only';
  document.getElementById('copy-summary').innerHTML =
    `Polymarket leaderboard · copy-trade candidates &nbsp;·&nbsp; ` +
    `${S.copy.length} ranked by <b>${cfg.metric||'profit'}</b>/${cfg.window||'all'} · ` +
    `<b style="color:var(--accent)">${copyable}</b> copyable &nbsp;·&nbsp; ${deep}`;
}

async function copyRescan() {
  if (!S.copyEnabled || S.copyScanning) return;
  S.copyScanning = true; renderCopy();
  const metric = document.getElementById('copy-metric').value;
  const window = document.getElementById('copy-window').value;
  try {
    const r = await fetch('/api/copytrade/scan', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({metric, window})}).then(r=>r.json());
    applyCopy(r);
  } catch(e){}
  S.copyScanning = false; renderCopy(); renderCopySummary();
}

// ── Log ───────────────────────────────────────────────────────────────────────
function addLog(e) {
  const cls = e.icon==='✗'?'err':e.icon==='✅'?'ok':e.icon==='→'?'dim':e.icon==='!'?'warn':e.icon==='◆'?'sig':'';
  const body = document.getElementById('log-body');
  const d = document.createElement('div');
  d.className = 'log-entry';
  d.innerHTML = `<span class="le-ts">${e.ts||''}</span><span class="le-icon">${e.icon||'·'}</span>`
    + `<span class="le-msg ${cls}">${esc(e.msg||'')}</span>`;
  body.insertBefore(d, body.firstChild);
  while (body.children.length > 150) body.removeChild(body.lastChild);
}
function toggleLog() {
  S.logExpanded = !S.logExpanded;
  document.getElementById('log-panel').classList.toggle('expanded', S.logExpanded);
  document.getElementById('log-toggle-btn').textContent = S.logExpanded ? 'Collapse' : 'Expand';
}

// ── Controls ──────────────────────────────────────────────────────────────────
async function startBot() {
  document.getElementById('slabel').textContent = 'STARTING';
  document.getElementById('sdot').className = 'sdot disc';
  await fetch('/api/start', {method:'POST'});
  connectSSE(); pollOnce();
}
async function stopBot() {
  _sseOn = false; if (es) { try{es.close();}catch(e){} es=null; }
  setConn(false); updateStatus('stopped');
  await fetch('/api/stop', {method:'POST'});
}

// fallback poll so panels fill immediately on load
async function pollOnce() {
  try {
    const sc = await fetch('/api/scalping').then(r=>r.json());
    S.scalp = sc.assets||{}; S.scalpSession = sc.session||{}; renderScalp(); renderScalpSummary();
  } catch(e){}
  try {
    const cp = await fetch('/api/copytrade').then(r=>r.json());
    applyCopy(cp);
  } catch(e){}
}

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

setInterval(() => {
  const fresh = S.lastDataTs && (Date.now() - S.lastDataTs) < 8000;
  document.getElementById('data-dot').classList.toggle('live', fresh);
  document.getElementById('data-label').textContent = fresh ? 'Live' : 'No data';
}, 1000);

// ── Boot ──────────────────────────────────────────────────────────────────────
connectSSE();
pollOnce();
setInterval(pollOnce, 5000);   // keep summaries warm even if a tab is hidden
