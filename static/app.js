// ── State ─────────────────────────────────────────────────────────────────────
const S = {
  tab: 'weather', botRunning: false,
  copy: [], copyCfg: {}, copyEnabled: false, copyScanning: false, copyExec: null,
  weather: [], weatherCfg: {}, weatherExec: null,
  lastDataTs: 0, logExpanded: false,
};

// ── Tabs ──────────────────────────────────────────────────────────────────────
const TABS = ['weather', 'copy'];
function showTab(t) {
  S.tab = t;
  TABS.forEach(x => {
    document.getElementById('tab-' + x).classList.toggle('active', t === x);
    document.getElementById('view-' + x).classList.toggle('active', t === x);
  });
  if (t === 'copy') renderCopy();
  if (t === 'weather') renderWeather();
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
    case 'copytrade':
      applyCopy(m); break;
    case 'weather':
      applyWeather(m); break;
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

// ── Weather render ────────────────────────────────────────────────────────────
function applyWeather(m) {
  S.weather = m.rows || [];
  S.weatherCfg = m.config || {};
  S.weatherExec = m.exec || null;
  S.lastDataTs = Date.now();
  renderWeatherSummary();
  renderWeatherExec();
  if (S.tab === 'weather') renderWeather();
}

function wxSigClass(sig) {
  if (sig === 'ENTER') return 'enter';
  if (sig === 'PRICED' || sig === 'THIN-EDGE' || sig === 'WIDE') return 'blocked';
  if (sig === 'NO-LOCK' || sig === 'EARLY') return 'settling';
  return 'flat';
}

function renderWeather() {
  const body = document.getElementById('weather-body');
  if (!S.weather.length) {
    body.innerHTML = '<tr><td colspan="8"><div class="no-data">No temperature markets found (or engine still warming up).</div></td></tr>';
    return;
  }
  // engine sorts into signal groups; insert a header whenever the group changes
  const GROUP_LABEL = {
    'actionable':     ['⚡ Actionable', 'passes every gate — the bot trades these'],
    'market-blocked': ['Blocked by the market', 'model likes it; price, spread or depth says no'],
    'not-yet':        ['Weather not settled', "the day's extreme isn't locked in yet"],
    'no-data':        ['No data', 'not enough observations to judge'],
    'other-day':      ['Other days', "not this station's local today"],
    'untradeable':    ['Not tradeable', 'no climatology / unsupported settlement source'],
  };
  let lastGroup = null;
  body.innerHTML = S.weather.map(r => {
    let hdr = '';
    if (r.group !== lastGroup) {
      lastGroup = r.group;
      const n = S.weather.filter(x => x.group === r.group).length;
      const [lbl, hint] = GROUP_LABEL[r.group] || [r.group, ''];
      hdr = `<tr class="grp ${r.group === 'actionable' ? 'grp-hot' : ''}">
               <td colspan="8"><span class="grp-l">${lbl}</span>
               <span class="grp-n">${n}</span>
               <span class="grp-h">${esc(hint)}</span></td></tr>`;
    }
    return hdr + rowHtml(r);
  }).join('');
}

function rowHtml(r) {
    const dim = (!r.is_today || !r.tradeable) ? ' style="opacity:.55"' : '';
    let localH = '—';
    if (r.local_hour != null) {
      const hh = Math.floor(r.local_hour), mm = Math.round((r.local_hour % 1) * 60);
      localH = String(mm === 60 ? hh + 1 : hh).padStart(2,'0') + ':' + String(mm === 60 ? 0 : mm).padStart(2,'0');
    }
    const best = r.buckets && r.buckets.length ?
      (r.buckets.find(b => b.label === r.best_label) || null) : null;
    const bidask = best ? `${best.bid_c != null ? best.bid_c.toFixed(0)+'¢' : '—'} / ${best.ask_c != null ? best.ask_c.toFixed(0)+'¢' : '—'}` : '—';
    const edge = best && best.edge_c != null ? (best.edge_c>0?'+':'')+best.edge_c+'¢' : '—';
    const edgeCls = best && best.edge_c != null ? (best.edge_c > 0 ? 'up' : 'dn') : '';
    const sc = wxSigClass(r.signal);
    const arrow = r.kind === 'low' ? '<span class="dn">▼</span>' : '<span class="up">▲</span>';
    return `<tr${dim}>
      <td class="l"><div class="match">${esc(r.city)} ${arrow}</div>
        <div class="kickoff">${esc(r.station||'?')} · ${esc(r.date||'')} · ${r.kind === 'low' ? 'LOW' : 'HIGH'}${r.tradeable?'':' · '+esc(r.why||'')}</div></td>
      <td>${localH}</td>
      <td>${r.temp_c != null ? r.temp_c.toFixed(0)+'°' : '—'} / <b>${r.ext_c != null ? r.ext_c.toFixed(0)+'°'+(r.unit||'C') : '—'}</b></td>
      <td>${r.best_label ? esc(r.best_label) : '—'}</td>
      <td>${r.best_p != null ? (r.best_p*100).toFixed(1)+'%' : '—'}</td>
      <td>${bidask}</td>
      <td><span class="${edgeCls}">${edge}</span></td>
      <td><span class="sig ${sc}" title="${esc(r.why||'')}">${esc(r.signal||'—')}</span></td>
    </tr>`;
}

function renderWeatherSummary() {
  const cfg = S.weatherCfg || {};
  const today = S.weather.filter(r => r.is_today && r.tradeable);
  const enters = S.weather.filter(r => r.signal === 'ENTER').length;
  document.getElementById('weather-summary').innerHTML =
    `Polymarket daily-high temp · NEAR-LOCK vs live METAR &nbsp;·&nbsp; ` +
    `${S.weather.length} markets · ${today.length} live today · ` +
    `<b style="color:var(--accent)">${enters}</b> ENTER signals &nbsp;·&nbsp; ` +
    `gates: p≥${cfg.p_min||'—'} · ask≤${cfg.price_max_c||'—'}¢ · edge≥${cfg.min_edge_c||'—'}¢ · local≥${cfg.min_local_hour||'—'}h`;
}

async function setWeatherMode(m) {
  const e = S.weatherExec || {};
  if (m === 'live') {
    if (!e.env_armed) { alert('Live is locked. Set WEATHER_LIVE=true and restart to arm.'); return; }
    if (!confirm('Switch to LIVE — the bot will place REAL orders with real USDC on the next qualifying signal. Continue?')) return;
  }
  try {
    const r = await fetch('/api/weather_config', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({mode:m})}).then(r=>r.json());
    S.weatherExec = r; renderWeatherExec();
  } catch(err){ alert('Could not change mode: ' + err); }
}

function renderWeatherExec() {
  const e = S.weatherExec; if (!e) return;
  const s = e.session || {};
  const mode = document.getElementById('wxexec-mode');
  if (e.live)          { mode.textContent = '🔴 LIVE forward-test — REAL orders'; mode.className = 'exec-arm live'; }
  else if (e.env_armed){ mode.textContent = '🟢 armed · paper (flip to Live to trade)'; mode.className = 'exec-arm ok'; }
  else                 { mode.textContent = '📄 PAPER · WEATHER_LIVE not set'; mode.className = 'exec-arm'; }
  // toggle button state (Live disabled until WEATHER_LIVE=true arms the env gate)
  const pb = document.getElementById('wx-paper'), lb = document.getElementById('wx-live');
  if (pb && lb) {
    pb.classList.toggle('active', !e.live);
    lb.classList.toggle('active', !!e.live);
    lb.disabled = !e.env_armed;
    lb.title = e.env_armed ? 'Place REAL orders' : 'Locked — set WEATHER_LIVE=true and restart to arm';
    lb.style.opacity = e.env_armed ? '' : '.5';
    lb.style.cursor = e.env_armed ? 'pointer' : 'not-allowed';
  }
  // per-mode counts (paper and live are separate books)
  const bm = e.by_mode || {live:{}, paper:{}};
  const wr = d => (d.settled_held ? Math.round(100*d.wins_held/d.settled_held)+'% ('+d.settled_held+')' : '—');
  const set = (id,v) => { const el=document.getElementById(id); if(el) el.textContent=v; };
  set('wx-live-open',    bm.live.open||0);   set('wx-live-settled',  bm.live.settled||0);
  set('wx-live-wr',      wr(bm.live||{}));
  set('wx-paper-open',   bm.paper.open||0);  set('wx-paper-settled', bm.paper.settled||0);
  set('wx-paper-wr',     wr(bm.paper||{}));
  document.getElementById('wx-avgp').textContent = e.avg_model_p == null ? '—' : Math.round(e.avg_model_p*100) + '%';
  const pnl = document.getElementById('wx-pnl');
  pnl.textContent = (s.realized_pnl >= 0 ? '+$' : '-$') + Math.abs(s.realized_pnl||0).toFixed(2);
  pnl.style.color = (s.realized_pnl||0) >= 0 ? 'var(--ok)' : 'var(--down)';
  if (s.realized_gross != null) pnl.title = `gross $${(s.realized_gross||0).toFixed(2)} − fees $${(s.fees_paid||0).toFixed(2)}`;
  const feesEl = document.getElementById('wx-fees');
  if (feesEl) feesEl.textContent = '$' + (s.fees_paid || 0).toFixed(2);
  document.getElementById('wx-staked').textContent = '$' + (s.staked_usd || 0);

  // REAL on-chain P&L — only shown once live trading has a baseline
  const a = e.account;
  const rBox = document.getElementById('wx-real-box'), uBox = document.getElementById('wx-usdc-box');
  if (a && rBox && uBox) {
    rBox.style.display = ''; uBox.style.display = '';
    const rp = document.getElementById('wx-realpnl');
    rp.textContent = (a.real_pnl >= 0 ? '+$' : '-$') + Math.abs(a.real_pnl).toFixed(2);
    rp.style.color = a.real_pnl >= 0 ? 'var(--ok)' : 'var(--down)';
    rp.title = `equity $${a.equity.toFixed(2)} = USDC $${a.usdc.toFixed(2)} + open marked-to-bid $${(a.open_value ?? a.open_cost).toFixed(2)}`
      + ` (cost $${a.open_cost.toFixed(2)}, unrealized $${(a.unrealized ?? 0).toFixed(2)}) − baseline $${a.baseline.toFixed(2)}`;
    document.getElementById('wx-usdc').textContent = '$' + a.usdc.toFixed(2);
    // flag when modeled and real disagree by more than a cent or two
    const gap = a.real_pnl - (s.realized_pnl || 0);
    rp.textContent += Math.abs(gap) >= 0.02 ? ` (${gap >= 0 ? '+' : ''}${gap.toFixed(2)} vs modeled)` : '';
  } else if (rBox && uBox) {
    rBox.style.display = 'none'; uBox.style.display = 'none';
  }

  const box = document.getElementById('wxexec-pos');
  const open = e.open || [];
  // Always render the section — "no active trades" is information, and a panel
  // that vanishes when empty reads as broken rather than idle.
  const nLive = open.filter(p => p.mode === 'live').length;
  const hd = `<div class="pos-hd">Active trades`
           + (open.length ? ` <span class="pos-count">${open.length}`
               + (nLive ? ` · ${nLive} live` : '') + `</span>` : '')
           + `</div>`;
  if (!open.length) {
    box.innerHTML = hd + '<div class="pos-empty">No active positions — '
      + (e.live ? 'armed and waiting for a signal' : 'paper mode') + '</div>';
    return;
  }
  box.innerHTML = hd + open.map(p => {
    const h = p.health || {};
    const broke = (h.breaks || []).length > 0;
    // live health: is the thesis we bought on still true?
    let hs = '';
    if (h.p_now != null) {
      const dcls = (h.p_delta ?? 0) < -0.02 ? 'dn' : '';
      hs = `<span class="pos-chip ${broke ? 'dn' : ''}" title="${esc((h.breaks||[]).join('; ') || 'thesis intact')}">`
         + `${broke ? '⚠ ' : ''}p ${p.model_p}→<span class="${dcls}">${h.p_now}</span>`
         + (h.headroom != null ? ` · room ${h.headroom > 0 ? '+' : ''}${h.headroom}°` : '')
         + ` · ${h.locked ? 'locked' : 'UNLOCKED'}</span>`;
    } else {
      hs = `<span class="pos-chip">p ${p.model_p} · +${p.edge_c}¢</span>`;
    }
    const mk = p.mark_c != null
      ? `<span class="pos-chip ${p.mark_c < p.entry_c ? 'dn' : 'up'}">bid ${p.mark_c}¢</span>` : '';
    return `<div class="pos-row ${p.mode}">
       <span class="pos-mode">${p.mode === 'live' ? 'LIVE' : 'PAPER'}</span>
       <span class="pos-match">${esc(p.city)} ${p.kind === 'low' ? '▼' : '▲'} ${esc(p.date)}</span>
       <span class="pos-buy">${esc(p.label)} @ ${p.entry_c}¢ ×${p.shares}</span>
       <span class="pos-cost">$${p.cost_usd}</span>
       ${mk}${hs}
     </div>`;
  }).join('');
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
    const cp = await fetch('/api/copytrade').then(r=>r.json());
    applyCopy(cp);
  } catch(e){}
  try {
    const wx = await fetch('/api/weather').then(r=>r.json());
    applyWeather(wx);
  } catch(e){}
}

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

setInterval(() => {
  // weather/copytrade push on 60s cadence — anything within ~1.5 cycles is live
  const fresh = S.lastDataTs && (Date.now() - S.lastDataTs) < 90000;
  document.getElementById('data-dot').classList.toggle('live', fresh);
  document.getElementById('data-label').textContent = fresh ? 'Live' : 'No data';
}, 1000);

// ── Boot ──────────────────────────────────────────────────────────────────────
connectSSE();
pollOnce();
setInterval(pollOnce, 5000);   // keep summaries warm even if a tab is hidden
