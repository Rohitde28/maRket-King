const API = 'http://localhost:8000';
let ws             = null;
let currentMode    = 'live';
let currentSymbol  = 'ES';     // active symbol
let currentTF      = '1m';     // active timeframe (1m default — spec primary)
let historyFilter  = 'ALL';    // signal list TF filter
let signalHistory  = [];
let stats          = { total: 0, wins: 0, losses: 0, tp1: 0, tp2: 0, sl: 0, sumR: 0 };

// ── Symbol / Mode / TF ────────────────────────────────────────────
function setSymbol(sym) {
  currentSymbol = sym;
  ['ES','MES','NQ'].forEach(s =>
    document.getElementById('sym-'+s).classList.toggle('active', s === sym));
  updateRightHeader();
  
// ── Matrix card selection ──────────────────────────────────────────
function selectCard(sym, tf) {
  currentSymbol = sym;
  currentTF     = tf;
  // Update topbar buttons
  ['ES','MES','NQ'].forEach(s => document.getElementById('sym-'+s).classList.toggle('active', s===sym));
  ['1m','5m'].forEach(t => document.getElementById('tf-'+t).classList.toggle('active', t===tf));
  // Highlight selected card
  document.querySelectorAll('.matrix-card').forEach(c => c.classList.remove('selected'));
  const card = document.getElementById('card-'+sym+'-'+tf);
  if (card) card.classList.add('selected');
  updateRightHeader();
  fetchAndShowSignal(sym, tf);
}

// ── Handle matrix_update heartbeat ────────────────────────────────
function handleMatrixUpdate(matrix) {
  for (const sym in matrix) {
    for (const tf in matrix[sym]) {
      const d = matrix[sym][tf];
      const key = sym + '-' + tf;
      const el_price   = document.getElementById('price-'   + key);
      const el_sig     = document.getElementById('sig-'     + key);
      const el_factors = document.getElementById('factors-' + key);
      const el_dir     = document.getElementById('dir-'     + key);
      const card       = document.getElementById('card-'    + key);
      if (el_price && d.price) el_price.textContent = parseFloat(d.price).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
      if (el_sig) {
        const ss = d.signal_strength || 'WAITING';
        el_sig.textContent = ss;
        el_sig.className = 'card-sig ' + ss;
        if (ss === 'ENTER' && card) {
          card.classList.add('flash-red');
          setTimeout(() => card.classList.remove('flash-red'), 8000);
        }
      }
      if (el_factors) el_factors.textContent = 'F: ' + (d.factors_hit||0) + '/4';
      if (el_dir && d.direction && d.direction !== 'none') {
        el_dir.textContent = d.direction === 'long' ? '▲ LONG' : '▼ SHORT';
        el_dir.style.color = d.direction === 'long' ? '#22c55e' : '#ef4444';
      } else if (el_dir) { el_dir.textContent = ''; }
    }
  }
}

fetchAndShowSignal(currentSymbol, currentTF);
}

function setMode(m) {
  currentMode = m;
  document.getElementById('btn-live').classList.toggle('active', m === 'live');
  document.getElementById('btn-backtest').classList.toggle('active', m === 'backtest');
  document.getElementById('view-live').style.display     = m === 'live'     ? '' : 'none';
  document.getElementById('view-backtest').style.display = m === 'backtest' ? '' : 'none';
  updateRightHeader();
}

function setTF(tf) {
  currentTF = tf;
  document.querySelectorAll('.tf').forEach(b => b.classList.remove('active'));
  document.getElementById('tf-'+tf).classList.add('active');
  updateRightHeader();
  fetchAndShowSignal(currentSymbol, currentTF);
}

function updateRightHeader() {
  const hdr = document.getElementById('right-hdr');
  if (hdr) hdr.textContent =
    (currentMode==='live'?'LIVE':'BACKTEST') + ' — ' + currentSymbol + ' — ' + currentTF;
}

// ── NY Clock ──────────────────────────────────────────────────────
// Windows in ET: 09:43-09:47 and 09:58-10:02
const WINDOWS_ET_MINS = [
  [9*60+43, 9*60+47],
  [9*60+58, 10*60+2]
];

// ── Update Clocks (Dynamic DST handling) ────────────────────────
function updateClock() {
  const now = new Date();
  
  // New York (ET)
  const nyStr = now.toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour12: false });
  document.getElementById('ny-time').innerText = nyStr;

  // India (IST)
  const istStr = now.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false });
  document.getElementById('ist-time').innerText = istStr;

  // NY Session Logic (Source of Truth)
  const etNow = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }));
  const etH = etNow.getHours();
  const etM = etNow.getMinutes();
  const etTotal = etH * 60 + etM;
  const wday = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', weekday: 'short' }).format(now);

  const winMsg = document.getElementById('window-msg');
  const nextVal = document.getElementById('next-window-ist');
  const cdVal = document.getElementById('countdown');
  const progress = document.getElementById('countdown-fill');

  if (wday === 'Sat' || wday === 'Sun') {
    winMsg.innerText = "Market Closed (Weekend)";
    nextVal.innerText = "Mon Open";
    cdVal.innerText = "--";
    progress.style.width = '0%';
    document.getElementById('weekend-overlay').style.display = 'block';
    return;
  }
  document.getElementById('weekend-overlay').style.display = 'none';

  // Windows in ET: 9:45 (W1), 10:00 (W2 - strongest), 10:30 (Extended)
  const windows = [ 
    { time: 585, label: "W1 - 09:45 ET" }, 
    { time: 600, label: "W2 - 10:00 ET (STRONGEST)" }, 
    { time: 630, label: "EXT - 10:30 ET" } 
  ];
  const currentWindow = windows.find(w => etTotal >= (w.time-2) && etTotal <= (w.time+2));
  
  if (currentWindow) {
    winMsg.innerText = `🟢 ACTIVE ${currentWindow.label}...`;
    cdVal.innerText = "NOW";
    progress.style.width = '100%';
    progress.style.background = '#f0b429';
  } else {
    let next = windows.find(w => etTotal < w.time);
    if (!next) {
      nextVal.innerText = "Tom. Open";
      cdVal.innerText = "--";
      winMsg.innerText = "Session Finished";
    } else {
      const diff = next.time - etTotal;
      // Convert next window ET to IST for display
      const targetET = new Date(etNow);
      targetET.setHours(Math.floor(next.time/60), next.time%60, 0);
      const istTimeStr = targetET.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute:'2-digit', hour12: false });
      
      nextVal.innerText = istTimeStr + " IST";
      cdVal.innerText = diff + "m left";
      winMsg.innerText = `Preparing for ${next.label}...`;
      progress.style.width = (Math.max(0, 60-diff)/60 * 100) + '%';
      progress.style.background = '#333';
    }
  }
}
setInterval(updateClock, 1000);
updateClock();

// ── WebSocket ─────────────────────────────────────────────────────
let pingInterval = null;

function connectWS() {
  if (ws) {
    ws.onclose = null;
    ws.onerror = null;
    ws.close();
  }
  ws = new WebSocket('ws://' + window.location.hostname + ':8000/ws/signals');

  ws.onopen = () => {
    document.getElementById('conn-status').textContent = '● LIVE';
    document.getElementById('conn-status').className   = 'ok';
    if (pingInterval) clearInterval(pingInterval);
    pingInterval = setInterval(() => ws && ws.readyState === 1 && ws.send('ping'), 20000);
  };

  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'signal')        handleSignal(msg.data);
      if (msg.type === 'outcome')       handleOutcome(msg.data);
      if (msg.type === 'matrix_update') handleMatrixUpdate(msg.data);
    } catch(_) {}
  };

  ws.onclose = () => {
    document.getElementById('conn-status').textContent = '● DISCONNECTED';
    document.getElementById('conn-status').className   = 'err';
    if (pingInterval) clearInterval(pingInterval);
    setTimeout(connectWS, 3000);
  };

  ws.onerror = () => ws.close();
}

// ── Signal handler ────────────────────────────────────────────────
function handleSignal(d) {
  if (d.signal_strength === 'ENTER') { addToHistory(d); updateStats(d); }
  const symMatch = !d.symbol || d.symbol.toUpperCase() === currentSymbol;
  if (!symMatch || d.timeframe !== currentTF) return;
  const el = document.getElementById('sig-strength');
  const prob = Math.round((d.final_probability||0)*100);
  el.textContent = d.signal_strength === 'NO_TRADE' ? 'NO TRADE' : d.signal_strength;
  el.className   = d.signal_strength;
  document.getElementById('sig-prob').textContent = d.signal_strength==='NO_TRADE' ? '' : prob+'%';
  document.getElementById('sig-dir').textContent =
    (d.direction && d.direction!=='none')
      ? 'Dir: '+d.direction.toUpperCase()+' | ATR: '+(d.atr||'--')
      : 'Direction: --';
  document.getElementById('s-entry').innerHTML = d.entry_range
    ? '<span style="color:#00ff88">'+d.entry_range.low+' – '+d.entry_range.high+'</span>'
    : (d.entry_price ? d.entry_price.toFixed(2) : '--');
  document.getElementById('s-sl').textContent  = d.stop_loss ? d.stop_loss.toFixed(2) : '--';
  document.getElementById('s-tp1').textContent = d.tp1       ? d.tp1.toFixed(2)       : '--';
  document.getElementById('s-tp2').textContent = d.tp2       ? d.tp2.toFixed(2)       : '--';
  document.getElementById('outcome-box').style.display = 'none';
  (d.factors||[]).forEach((f,i) => {
    const id = ['f1','f2','f3','f4'][i]; if(!id) return;
    document.getElementById(id+'-dot').className = 'factor-dot '+(f.satisfied?'ok':'fail');
    const det = document.getElementById(id+'-det');
    det.textContent = (f.detail||'--').replace('✓ ',''); det.title = f.detail||'';
  });
  document.getElementById('last-analysis-box').style.display = '';
  document.getElementById('last-analysis-json').textContent =
    JSON.stringify({signal_strength:d.signal_strength,factors_hit:d.factors_hit,
      final_probability:d.final_probability,direction:d.direction,
      symbol:d.symbol,timeframe:d.timeframe,notes:d.notes},null,2);
}

// ── Outcome handler ───────────────────────────────────────────────
function handleOutcome(d) {
  const outcome = d.outcome;
  const ob = document.getElementById('outcome-box');
  ob.style.display = '';
  if (outcome === 'SL') {
    ob.style.background = '#3a0d0d';
    ob.style.color = '#ef4444';
    ob.style.border = '1px solid #ef4444';
    ob.textContent = `❌ STOP LOSS HIT @ ${d.exit_price?.toFixed(2) || '--'}`;
  } else if (outcome === 'TP2') {
    ob.style.background = '#0d2b0d';
    ob.style.color = '#22c55e';
    ob.style.border = '1px solid #22c55e';
    ob.textContent = `✅ TP2 HIT @ ${d.exit_price?.toFixed(2) || '--'} (+3R)`;
  } else if (outcome === 'TP1') {
    ob.style.background = '#2b2b0d';
    ob.style.color = '#f0b429';
    ob.style.border = '1px solid #f0b429';
    ob.textContent = `✅ TP1 HIT @ ${d.exit_price?.toFixed(2) || '--'} (+2R)`;
  }

  // Update history row
  updateHistoryOutcome(d);
  // Update stats based on outcome
  if (outcome === 'SL')  { stats.losses++; stats.sumR -= 1; }
  if (outcome === 'TP1') { stats.wins++;   stats.sumR += 2; }
  if (outcome === 'TP2') { stats.wins++;   stats.sumR += 3; }
  renderStats();
}

// ── History table ─────────────────────────────────────────────────
function addToHistory(d) {
  if (d.signal_strength !== 'ENTER') return;
  if (signalHistory.find(r => r.id === d._db_id && d._db_id)) return;
  let timeStr = '--';
  if (d.timestamp) {
    try { timeStr = new Date(d.timestamp).toLocaleTimeString('en-IN',
      {timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',hour12:false})+' IST';
    } catch(e) {}
  }
  signalHistory.unshift({
    id: d._db_id || Date.now(),
    timeStr, sym: (d.symbol||'ES').toUpperCase(), tf: d.timeframe||'1m',
    dir: d.direction||'--', sig: d.signal_strength,
    prob: Math.round((d.final_probability||0)*100),
    outcome:'OPEN', entry:d.entry_price,
    entry_range:d.entry_range, sl:d.stop_loss, tp1:d.tp1, tp2:d.tp2,
  });
  if (signalHistory.length > 100) signalHistory.pop();
  renderHistory();
}

function updateHistoryOutcome(d) {
  const row = signalHistory.find(r => r.id === d._db_id);
  if (row) { row.outcome = d.outcome; renderHistory(); }
}

function setHistoryFilter(f) {
  historyFilter = f;
  document.querySelectorAll('.hf-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.f === f));
  renderHistory();
}

function renderHistory() {
  const tbody = document.getElementById('history-body');
  let rows = signalHistory;
  if (historyFilter !== 'ALL') rows = rows.filter(r => r.tf === historyFilter);
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="color:#444;text-align:center;padding:20px">No signals yet</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td style="white-space:nowrap;color:#888">${r.timeStr}</td>
      <td style="color:#f0b429;font-weight:bold">${r.sym}</td>
      <td style="color:#aaa">${r.tf}</td>
      <td style="color:${r.dir==='long'?'#22c55e':'#ef4444'}">${r.dir}</td>
      <td><span class="badge ${r.sig}">${r.sig}</span> <span style="color:#888">${r.prob}%</span></td>
      <td style="color:#00ff88">${r.entry_range?(r.entry_range.low+'-'+r.entry_range.high):(r.entry?r.entry.toFixed(2):'--')}</td>
      <td style="color:#ef4444">${r.sl?r.sl.toFixed(2):'--'}</td>
      <td><span class="badge ${r.outcome}">${r.outcome}</span></td>
    </tr>`).join('');
}

// ── Stats ─────────────────────────────────────────────────────────
function updateStats(d) {
  stats.total++;
  // New signal starts OPEN — wins/losses updated on outcome
  renderStats();
}

function renderStats() {
  document.getElementById('st-total').textContent = stats.total;
  const closed = stats.wins + stats.losses;
  const wr = closed > 0 ? Math.round(stats.wins / closed * 100) : '--';
  document.getElementById('st-wr').textContent  = closed > 0 ? wr + '%' : '--%';
  document.getElementById('st-tp1').textContent = stats.tp1;
  document.getElementById('st-tp2').textContent = stats.tp2;
  document.getElementById('st-sl').textContent  = stats.losses;
}

// ── Fetch latest signal for selected TF ───────────────────────────
async function fetchAndShowSignal(sym, tf) {
  try {
    const r = await fetch(`${API}/api/signal?symbol=${sym}&timeframe=${tf}`);
    if (r.ok) handleSignal(await r.json());
  } catch(_) {}
}

// ── Load history from DB ──────────────────────────────────────────
async function loadHistory() {
  try {
    const r = await fetch(`${API}/api/signals/history?limit=30`);
    if (!r.ok) { console.error("History fetch failed", r.status); return; }
    const data = await r.json();
    data.forEach(d => {
      let timeStr = '--';
      if (d.timestamp) {
        try {
          timeStr = new Date(d.timestamp).toLocaleTimeString('en-IN',
            { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: false }) + " IST";
        } catch(e) { console.error("Bad date", d.timestamp); }
      }
      signalHistory.push({
        id: d.id, timeStr, tf: d.timeframe,
        dir: d.direction || '--',
        sig: d.signal_strength || 'WAITING',
        prob: Math.round((d.final_probability || 0) * 100),
        entry: d.entry_price, sl: d.stop_loss, tp1: d.tp1, tp2: d.tp2,
        outcome: d.outcome || 'OPEN',
      });
    });
    renderHistory();
  } catch(err) {
    console.error("loadHistory error:", err);
  }

  try {
    const r = await fetch(`${API}/api/stats`);
    if (!r.ok) return;
    const s = await r.json();
    stats.total   = s.signals_total || s.total_trades || 0;
    stats.wins    = (s.signals_tp1 || 0) + (s.signals_tp2 || 0) || s.wins || 0;
    stats.losses  = s.signals_sl || s.losses || 0;
    stats.tp1     = s.signals_tp1 || s.tp1_hits || 0;
    stats.tp2     = s.signals_tp2 || s.tp2_hits || 0;
    stats.sumR    = (stats.wins * 2) - stats.losses;  // rough estimate
    renderStats();
  } catch(err) {
    console.error("stats error:", err);
  }
}

// ── Backtest ──────────────────────────────────────────────────────
async function runBacktest() {
  const btn = document.getElementById('run-backtest-btn');
  const out = document.getElementById('backtest-output');
  const tf  = document.getElementById('bt-tf').value;
  const sym = document.getElementById('bt-symbol').value;
  const min = document.getElementById('bt-minsig').value;
  const date= document.getElementById('bt-date').value;
  const end = document.getElementById('bt-end-date').value;

  btn.disabled = true;
  btn.textContent = '⏳ Running...';
  out.textContent = `Running backtest: ${tf} | range: ${date || 'recent'} to ${end || 'now'} | strictness: ${min}\nThis may take 10-30 seconds...\n`;

  let url = `${API}/api/backtest?timeframe=${tf}&min_signal=${min}&symbol=${sym}`;
  if (date) url += `&start=${date}`;
  if (end)  url += `&end=${end}`;
  if (!date && !end) url += `&bars=400`;

  try {
    const r = await fetch(url, { method: 'POST' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    
    if (data.error) {
       throw new Error(data.error);
    }

    const s = data.summary;
    const config = data.config;

    // 1. Render Summary Header
    const sumBox = document.getElementById('bt-summary-box');
    sumBox.style.display = 'block';
    sumBox.innerHTML = `
      <div style="display:grid; grid-template-columns: repeat(5, 1fr); gap:10px; background:#111; padding:15px; border-radius:4px; border:1px solid #222">
        <div><small style="color:#666">Signals</small><div style="font-size:18px">${s.total_trades}</div></div>
        <div><small style="color:#666">Win Rate</small><div style="font-size:18px; color:${s.win_rate > 0.5 ? '#27ae60':'#e74c3c'}">${(s.win_rate * 100).toFixed(1)}%</div></div>
        <div><small style="color:#666">Brokerage</small><div style="font-size:18px; color:#e67e22">-$${s.total_brokerage}</div></div>
        <div><small style="color:#666">P&L (Net)</small><div style="font-size:18px; color:${s.total_pnl_usd >= 0 ? '#27ae60':'#e74c3c'}">${s.total_pnl_usd >= 0 ? '$'+s.total_pnl_usd.toLocaleString() : '-$'+Math.abs(s.total_pnl_usd).toLocaleString()}</div></div>
        <div><small style="color:#666">TP1 / TP2</small><div style="font-size:18px">${s.tp1_hits} / ${s.tp2_hits}</div></div>
      </div>
      <div style="font-size:11px; color:#444; margin-top:5px">
        Config: ${config.symbol} ${config.timeframe} | Timing: ${config.skip_timing ? 'Skipped':'Strict'} | Warmup: 50
      </div>
    `;

    // 2. Render Table Rows
    const tbody = document.getElementById('bt-table-body');
    tbody.innerHTML = '';
    document.getElementById('bt-table-box').style.display = 'block';

    (data.trades || []).forEach(t => {
      const tr = document.createElement('tr');
      const isWin = t.outcome.startsWith('TP');
      const isLoss = t.outcome === 'SL';
      tr.className = isWin ? 'bt-row-win' : (isLoss ? 'bt-row-loss' : 'bt-row-timeout');
      
      tr.innerHTML = `
        <td>${t.bar_time}</td>
        <td><span class="bt-tag tag-strength">${t.signal}</span> <small style="color:#555">F:${t.factors}/4</small></td>
        <td style="color:${t.dir === 'long' ? '#27ae60':'#e74c3c'}">${t.dir.toUpperCase()}</td>
        <td style="color:#00ff88">${t.entry_range ? (t.entry_range.low + ' - ' + t.entry_range.high) : t.entry}</td>
        <td><small style="color:#666">${t.sl} / ${t.tp1}</small></td>
        <td><span class="bt-tag ${isWin ? 'tag-win' : (isLoss ? 'tag-loss' : '')}">${t.outcome}</span></td>
        <td style="color:${t.pnl_usd >= 0 ? '#27ae60':'#e74c3c'}">${t.pnl_usd >= 0 ? '+$'+t.pnl_usd : '-$'+Math.abs(t.pnl_usd)} <small style="color:#444">(-$${t.brokerage} fee)</small></td>
      `;
      tbody.appendChild(tr);

      // Add diagnosis sub-row if failed
      if (t.diagnosis) {
        const diagRow = document.createElement('tr');
        diagRow.innerHTML = `<td colspan="6" style="background:#0a0a0a; color:#888; border-top:none; font-size:11px; padding:4px 15px;">
          <span style="color:#e67e22">DIAGNOSTIC:</span> ${t.diagnosis}
        </td>`;
        tbody.appendChild(diagRow);
      }
    });

    if (!data.trades || data.trades.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding:40px; color:#444">No signals fired for this period. Try a looser setup or larger timeframe.</td></tr>';
    }

    out.style.display = 'none'; // hide old pre
  } catch(e) {
    console.error("bt error:", e);
    out.style.display = 'block';
    out.textContent = `ERROR: ${e.message}\n\nMake sure the backend is running:\n  cd backend\n  uvicorn main:app --reload`;
    out.style.color = '#e74c3c';
  } finally {
    btn.disabled = false;
    btn.textContent = '▶ RUN BACKTEST';
  }
}

// ── Init ──────────────────────────────────────────────────────────
// Set default backtest date to 7 days ago
const d = new Date();
d.setDate(d.getDate() - 7);
document.getElementById('bt-date').value = d.toISOString().split('T')[0];

connectWS();
loadHistory();
fetchAndShowSignal(currentSymbol, currentTF);