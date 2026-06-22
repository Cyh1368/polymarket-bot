#!/usr/bin/env python3
"""Flask dashboard for polymarket_xrp_maker_trader — serves at 0.0.0.0:8097."""
from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request
from flask_cors import CORS

APP_DIR    = Path(__file__).resolve().parent
LIVE_JSON  = APP_DIR / "xrp_maker_live.json"
TRADES_CSV = APP_DIR / "xrp_maker_trades.csv"
LOG_PATH   = APP_DIR / "xrp_maker_trader.log"
BALANCE_HISTORY_JSON = APP_DIR / "xrp_maker_balance_history.json"

app = Flask(__name__)
CORS(app)


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_session() -> dict:
    if LIVE_JSON.exists():
        try:
            return json.loads(LIVE_JSON.read_text())
        except Exception:
            pass
    return {}


def load_trades() -> list[dict]:
    if not TRADES_CSV.exists():
        return []
    try:
        with open(TRADES_CSV) as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def load_log_tail(n: int = 300) -> list[str]:
    if not LOG_PATH.exists():
        return []
    try:
        with open(LOG_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 131072))  # last 128 KB
            tail = f.read().decode("utf-8", errors="replace")
        return tail.splitlines()[-n:]
    except Exception:
        return []


def compute_stats(trades: list[dict]) -> dict:
    entries  = [t for t in trades if t.get("event") == "order_placed"]
    settled  = [t for t in trades if t.get("event") == "settled"]

    wins = losses = 0
    pnl_vals: list[float] = []
    per_contract_pnls: list[float] = []

    for t in settled:
        filled  = float(t.get("filled_shares") or 0)
        outcome = t.get("official_outcome")
        pnl_raw = t.get("net_pnl")

        if filled > 0 and pnl_raw not in (None, "", "0.0", "0"):
            pnl = float(pnl_raw)
            pnl_vals.append(pnl)
            per_contract_pnls.append(pnl / filled)
            if pnl > 0:
                wins += 1
            else:
                losses += 1

    cumulative_pnl = sum(pnl_vals)
    n_filled = wins + losses
    win_rate  = wins / n_filled * 100 if n_filled else None

    return {
        "n_entries":    len(entries),
        "n_settled":    len(settled),
        "n_filled":     n_filled,
        "wins":         wins,
        "losses":       losses,
        "win_rate":     win_rate,
        "cumulative_pnl": cumulative_pnl,
        "mean_pnl_per_contract": (
            sum(per_contract_pnls) / len(per_contract_pnls)
            if per_contract_pnls else None
        ),
        "pnl_history":  per_contract_pnls,
    }


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/log")
def get_log() -> Response:
    n = min(int(request.args.get("n", 400)), 400)
    return jsonify({"lines": load_log_tail(n)})


def load_balance_history() -> list[dict]:
    if not BALANCE_HISTORY_JSON.exists():
        return []
    try:
        return json.loads(BALANCE_HISTORY_JSON.read_text())
    except Exception:
        return []


@app.route("/stats")
def get_stats() -> Response:
    trades  = load_trades()
    session = load_session()
    stats   = compute_stats(trades)
    balance_history = load_balance_history()

    # Latest open/settled contract
    latest: dict = {}
    placed  = [t for t in trades if t.get("event") == "order_placed"]
    settled = [t for t in trades if t.get("event") == "settled"]
    if placed:
        p = placed[-1]
        latest["slug"]        = p.get("market_slug", "")
        latest["side"]        = p.get("side", "")
        latest["limit_price"] = p.get("limit_price", "")
        latest["size"]        = p.get("size_shares", "")
        latest["ev"]          = p.get("ev_no") if p.get("side") == "NO" else p.get("ev_yes")
        # Find matching settle
        match = next((s for s in reversed(settled)
                      if s.get("condition_id") == p.get("condition_id")), None)
        if match:
            latest["status"]   = match.get("order_status", "")
            latest["filled"]   = match.get("filled_shares", "")
            latest["outcome"]  = match.get("official_outcome", "")
            latest["net_pnl"]  = match.get("net_pnl", "")
        else:
            latest["status"] = "open"

    return jsonify({
        "stats":             stats,
        "session":           session,
        "latest":            latest,
        "balance_history":   balance_history,
    })


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

@app.route("/")
def index() -> Response:
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>XRP Maker Trader</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@3.9.1/dist/chart.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f0f14; color: #d0d0e0; font-family: 'Fira Mono', 'Consolas', monospace; font-size: 12.5px; min-height: 100vh; }
  header { background: #1a1a2e; padding: 14px 22px; display: flex; align-items: center; gap: 18px; border-bottom: 1px solid #2a2a4a; }
  header h1 { font-size: 16px; font-weight: 700; color: #a0c4ff; letter-spacing: 1px; }
  header .dot { width: 9px; height: 9px; border-radius: 50%; background: #22e08a; animation: pulse 1.8s infinite; }
  #mode-badge { font-size: 12px; font-weight: 700; padding: 3px 10px; border-radius: 4px; letter-spacing: 1px; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.3; } }
  .layout { display: grid; grid-template-columns: 1fr 340px; gap: 0; height: calc(100vh - 52px); }
  .log-pane { padding: 14px 18px; overflow-y: auto; border-right: 1px solid #1e1e38; }
  .sidebar  { display: flex; flex-direction: column; padding: 14px 16px; gap: 14px; overflow-y: auto; }
  pre#logbox { white-space: pre-wrap; word-break: break-all; line-height: 1.55; font-size: 11.5px; color: #c0c0d8; }
  pre#logbox .line { display: block; }
  pre#logbox .line:hover { background: rgba(160,196,255,0.06); }

  .card { background: #14142a; border: 1px solid #22224a; border-radius: 6px; padding: 12px 14px; }
  .card h2 { font-size: 10px; text-transform: uppercase; letter-spacing: 1.2px; color: #6060a0; margin-bottom: 10px; }
  .metric-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 12px; }
  .metric-item .label { font-size: 10px; color: #606090; margin-bottom: 2px; }
  .metric-item .value { font-size: 16px; font-weight: 700; }
  .metric-item .value.green { color: #22e08a; }
  .metric-item .value.red   { color: #ff6060; }
  .metric-item .value.gray  { color: #8080b0; }
  .metric-item .value.white { color: #d0d0e8; }
  .metric-item .value.blue  { color: #a0c4ff; }
  .metric-item .value.gold  { color: #ffe066; }

  .contract-box { font-size: 11px; color: #b0b0d0; line-height: 1.7; }
  .contract-box .slug    { font-size: 12px; color: #a0c4ff; font-weight: 700; margin-bottom: 4px; }
  .contract-box .win     { color: #22e08a; }
  .contract-box .loss    { color: #ff6060; }
  .contract-box .open    { color: #ffe066; }
  .contract-box .skipped { color: #8080a0; }
  .contract-box .side    { color: #c0e0c0; font-weight: 700; }

  .pnl-spark { width: 100%; height: 60px; }

  .log-pane::-webkit-scrollbar, .sidebar::-webkit-scrollbar { width: 5px; }
  .log-pane::-webkit-scrollbar-thumb, .sidebar::-webkit-scrollbar-thumb { background: #2a2a5a; border-radius: 3px; }
  .scroll-btm-btn { position: fixed; bottom: 22px; right: 360px; background: #222248; color: #a0c4ff;
    border: 1px solid #3a3a7a; border-radius: 14px; padding: 4px 14px; font-size: 11px; cursor: pointer; }
  .scroll-btm-btn:hover { background: #2a2a5a; }
</style>
</head>
<body>
<header>
  <div class="dot"></div>
  <h1>XRP MAKER TRADER</h1>
  <span id="mode-badge" style="background:rgba(255,220,60,0.12);color:#ffe066;border:1px solid #ffe06644;">--- DRY ---</span>
  <span id="header-status" style="margin-left:auto;font-size:11px;color:#606090;">loading...</span>
</header>

<div class="layout">
  <div class="log-pane" id="log-pane">
    <pre id="logbox"></pre>
  </div>

  <div class="sidebar">

    <div class="card">
      <h2>Session Stats</h2>
      <div class="metric-grid">
        <div class="metric-item">
          <div class="label">Cumulative PnL</div>
          <div class="value" id="cum-pnl">--</div>
        </div>
        <div class="metric-item">
          <div class="label">PnL / Contract</div>
          <div class="value" id="mean-pnl">--</div>
        </div>
        <div class="metric-item">
          <div class="label">Win Rate</div>
          <div class="value blue" id="win-rate">--</div>
        </div>
        <div class="metric-item">
          <div class="label">W / L</div>
          <div class="value white" id="wl">--</div>
        </div>
        <div class="metric-item">
          <div class="label">Signals / Fills</div>
          <div class="value gray" id="sig-fills">--</div>
        </div>
        <div class="metric-item">
          <div class="label">Contracts Seen</div>
          <div class="value gray" id="contracts-seen">--</div>
        </div>
        <div class="metric-item" style="grid-column:1/-1;">
          <div class="label">Uptime</div>
          <div class="value white" style="font-size:13px;" id="uptime">--</div>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>PnL History</h2>
      <svg id="spark" class="pnl-spark" viewBox="0 0 300 60" preserveAspectRatio="none"></svg>
    </div>

    <div class="card">
      <h2>Balance Over Time</h2>
      <canvas id="balanceChart" style="max-height:120px;"></canvas>
    </div>

    <div class="card">
      <h2>Latest Contract</h2>
      <div class="contract-box" id="contract-box"><span class="skipped">Waiting for data…</span></div>
    </div>

  </div>
</div>

<button class="scroll-btm-btn" onclick="scrollToBottom()">↓ latest</button>

<script>
const logPane = document.getElementById('log-pane');
const logbox  = document.getElementById('logbox');
let autoScroll = true;

logPane.addEventListener('scroll', () => {
  autoScroll = logPane.scrollTop + logPane.clientHeight >= logPane.scrollHeight - 30;
});
function scrollToBottom() { logPane.scrollTop = logPane.scrollHeight; autoScroll = true; }

function colorLine(t) {
  if (/SETTLED.*correct=True/i.test(t))   return {color: '#22e08a', bold: true};
  if (/SETTLED.*correct=False/i.test(t))  return {color: '#ff6060', bold: true};
  if (/SETTLED/i.test(t))                 return {color: '#d4b896'};
  if (/posting GTC/i.test(t))             return {color: '#ffe066', bold: true};
  if (/DRY-RUN/i.test(t))                 return {color: '#ffe066'};
  if (/EV.*below tau/i.test(t))           return {color: '#8080a0'};
  if (/no HF spot|stale book|skip/i.test(t)) return {color: '#8080a0'};
  if (/Watching xrp/i.test(t))            return {color: '#a0c4ff'};
  if (/CLOB book WS.*subscribed/i.test(t)) return {color: '#6060a0'};
  if (/T=\d+s.*um=/i.test(t))             return {color: '#c0a0ff'};
  if (/error|Error/i.test(t))             return {color: '#ff8060'};
  if (/=== XRP Maker/i.test(t))           return {color: '#a0c4ff', bold: true};
  if (/Model loaded/i.test(t))            return {color: '#d4b896'};
  if (/outcome not yet/i.test(t))         return {color: '#8080a0'};
  if (/retrying/i.test(t))               return {color: '#8080a0'};
  return {};
}

function updateLog(lines) {
  const frag = document.createDocumentFragment();
  lines.forEach(line => {
    const span = document.createElement('span');
    span.className = 'line';
    span.textContent = line;
    const s = colorLine(line);
    if (s.color) span.style.color = s.color;
    if (s.bold)  span.style.fontWeight = '700';
    frag.appendChild(span);
  });
  logbox.innerHTML = '';
  logbox.appendChild(frag);
  if (autoScroll) scrollToBottom();
}

function pnlColor(v) {
  if (v == null) return '#8080b0';
  return v > 0 ? '#22e08a' : v < 0 ? '#ff6060' : '#8080b0';
}
function fmt(v, decimals=4) {
  if (v == null) return '--';
  return (v >= 0 ? '+' : '') + v.toFixed(decimals);
}

function drawSparkline(history) {
  const svg = document.getElementById('spark');
  if (!history || history.length < 2) { svg.innerHTML = ''; return; }
  const W = 300, H = 60, pad = 4;
  const mn = Math.min(...history), mx = Math.max(...history);
  const rng = Math.max(mx - mn, 0.001);
  const pts = history.map((v, i) =>
    `${pad + i * (W-2*pad) / (history.length-1)},${pad + (1-(v-mn)/rng)*(H-2*pad)}`
  ).join(' ');
  const zeroY = pad + (1 - (0-mn)/rng) * (H-2*pad);
  svg.innerHTML =
    `<line x1="${pad}" y1="${Math.min(H-pad,Math.max(pad,zeroY))}" x2="${W-pad}" y2="${Math.min(H-pad,Math.max(pad,zeroY))}" stroke="#333" stroke-dasharray="3"/>` +
    `<polyline points="${pts}" fill="none" stroke="#22e08a" stroke-width="1.5"/>`;
}

function updateStats(data) {
  const s = data.stats || {}, sess = data.session || {}, lat = data.latest || {};

  const cumEl = document.getElementById('cum-pnl');
  cumEl.textContent = fmt(s.cumulative_pnl, 4);
  cumEl.className   = 'value ' + (s.cumulative_pnl > 0 ? 'green' : s.cumulative_pnl < 0 ? 'red' : 'gray');

  const meanEl = document.getElementById('mean-pnl');
  meanEl.textContent = s.mean_pnl_per_contract != null ? fmt(s.mean_pnl_per_contract, 4) : '--';
  meanEl.className   = 'value ' + (s.mean_pnl_per_contract > 0 ? 'green' : s.mean_pnl_per_contract < 0 ? 'red' : 'gray');

  const wrEl = document.getElementById('win-rate');
  wrEl.textContent = s.win_rate != null ? s.win_rate.toFixed(1) + '%' : '--';
  wrEl.style.color  = s.win_rate >= 60 ? '#22e08a' : s.win_rate >= 50 ? '#ffe066' : '#ff6060';

  document.getElementById('wl').textContent = `${s.wins ?? '--'} / ${s.losses ?? '--'}`;
  document.getElementById('sig-fills').textContent = `${s.n_entries ?? '--'} / ${s.n_filled ?? '--'}`;
  document.getElementById('contracts-seen').textContent = sess.contracts_seen ?? '--';

  const upS = sess.uptime_s || 0;
  document.getElementById('uptime').textContent =
    `${Math.floor(upS/3600)}h ${Math.floor((upS%3600)/60)}m  [since ${(sess.start_time_utc||'').slice(0,16)}]`;

  document.getElementById('header-status').textContent =
    `W:${s.wins||0} L:${s.losses||0} skip:${sess.trades_skipped||0} pnl:${fmt(s.cumulative_pnl,4)}`;

  drawSparkline(s.pnl_history || []);
  drawBalanceChart(data.balance_history || []);

  // Latest contract box
  let html = '';
  if (lat.slug) {
    const cls = lat.status === 'open' ? 'open' :
                (lat.net_pnl > 0) ? 'win' : (lat.net_pnl < 0) ? 'loss' : 'skipped';
    html += `<div class="slug">${lat.slug}</div>`;
    html += `<span class="side">${lat.side||'?'}</span> @ <b>${lat.limit_price||'?'}</b>  ×${lat.size||'?'} shares<br>`;
    if (lat.ev)      html += `ev: ${parseFloat(lat.ev) >= 0 ? '+' : ''}${parseFloat(lat.ev).toFixed(4)}<br>`;
    if (lat.status === 'open') {
      html += `<span class="open">● order open</span>`;
    } else if (lat.filled > 0 && lat.outcome !== '') {
      const out = lat.outcome === '1' ? 'Up ↑' : 'Down ↓';
      html += `filled: ${lat.filled}  outcome: <span class="outcome">${out}</span><br>`;
      html += `pnl: <span class="${cls}">${fmt(parseFloat(lat.net_pnl||0), 4)}</span>`;
    } else if (lat.filled > 0) {
      html += `<span class="open">filled=${lat.filled}, outcome pending</span>`;
    } else {
      html += `<span class="skipped">unfilled / expired</span>`;
    }
  } else {
    html = '<span class="skipped">Waiting for data…</span>';
  }
  document.getElementById('contract-box').innerHTML = html;
}

let balanceChartInstance = null;

function drawBalanceChart(history) {
  if (!history || history.length < 2) {
    document.getElementById('balanceChart').style.display = 'none';
    return;
  }
  document.getElementById('balanceChart').style.display = 'block';

  const ctx = document.getElementById('balanceChart').getContext('2d');
  const labels = history.map(h => {
    const dt = new Date(h.timestamp_utc + 'Z');
    return `${dt.getHours().toString().padStart(2,'0')}:${dt.getMinutes().toString().padStart(2,'0')}`;
  });
  const balances = history.map(h => parseFloat(h.balance || 0));

  const minBalance = Math.min(...balances);
  const maxBalance = Math.max(...balances);
  const yMin = Math.floor(minBalance * 0.95);
  const yMax = Math.ceil(maxBalance * 1.05);

  if (balanceChartInstance) balanceChartInstance.destroy();

  balanceChartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [{
        label: 'Account Balance ($)',
        data: balances,
        borderColor: '#22e08a',
        backgroundColor: 'rgba(34, 224, 138, 0.08)',
        borderWidth: 2,
        fill: true,
        tension: 0.2,
        pointRadius: 3,
        pointBackgroundColor: '#22e08a',
        pointBorderColor: '#0f0f14',
        pointBorderWidth: 1,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false }
      },
      scales: {
        y: {
          min: yMin,
          max: yMax,
          ticks: { color: '#6060a0', font: { size: 10 } },
          grid: { color: 'rgba(96,96,160,0.1)' }
        },
        x: {
          ticks: { color: '#6060a0', font: { size: 9 } },
          grid: { display: false }
        }
      }
    }
  });
}

async function poll() {
  try {
    const [logRes, statsRes] = await Promise.all([
      fetch('/log?n=400'), fetch('/stats')
    ]);
    const logData   = await logRes.json();
    const statsData = await statsRes.json();
    updateLog(logData.lines || []);
    updateStats(statsData);
  } catch (e) { console.warn('poll error', e); }
}

poll();
setInterval(poll, 3000);
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8097)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"XRP Maker display server: http://localhost:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
