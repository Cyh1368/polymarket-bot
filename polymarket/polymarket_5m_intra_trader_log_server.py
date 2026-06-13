#!/usr/bin/env python3
"""Flask dashboard for polymarket_5m_intra_trader — serves at 0.0.0.0:8100."""
from __future__ import annotations

import csv
import json
import math
import os
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, Response
from flask_cors import CORS

APP_DIR = Path(__file__).resolve().parent
LOG_PATH = Path(os.getenv("POLYMARKET_INTRA_TRADER_LOG", str(APP_DIR / "polymarket_5m_intra_trader.log")))
TRADES_CSV = Path(os.getenv("POLYMARKET_INTRA_TRADER_TRADES_CSV", str(APP_DIR / "polymarket_5m_intra_trader_trades.csv")))
PORTFOLIO_CSV = Path(os.getenv("POLYMARKET_INTRA_TRADER_PORTFOLIO_CSV", str(APP_DIR / "polymarket_5m_intra_trader_portfolio.csv")))

app = Flask(__name__)
CORS(app)


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def compute_stats() -> dict[str, Any]:
    rows = _read_csv_rows(TRADES_CSV)
    # intra-period trader records exits as "outcome" events
    outcome_rows = [r for r in rows if r.get("event") == "outcome"]
    decided_rows = [r for r in outcome_rows if str(r.get("correct", "")).strip() in ("0", "1")]
    total = len(outcome_rows)
    wins = sum(1 for r in outcome_rows if str(r.get("correct", "")).strip() == "1")
    losses = sum(1 for r in outcome_rows if str(r.get("correct", "")).strip() == "0")
    skipped = sum(1 for r in rows if r.get("event") == "decision" and r.get("order_status") == "skip")
    decided = wins + losses
    win_rate = wins / decided if decided > 0 else None

    # Average pnl ratio for decided trades
    pnl_values = [_finite(r.get("pnl_ratio")) for r in decided_rows if r.get("pnl_ratio")]
    avg_pnl = sum(pnl_values) / len(pnl_values) if pnl_values else None

    # Entry ask prices for break-even p-value (Poisson-Binomial)
    ask_probs = [_finite(r.get("selected_ask")) for r in decided_rows]
    ask_probs = [p for p in ask_probs if 0.0 < p < 1.0]
    breakeven_p = _poisson_binomial_pvalue(wins, ask_probs) if len(ask_probs) == decided and decided > 0 else None

    return {
        "total_decided": total,
        "wins": wins,
        "losses": losses,
        "skipped": skipped,
        "win_rate": round(win_rate * 100, 1) if win_rate is not None else None,
        "avg_pnl_ratio": round(avg_pnl, 4) if avg_pnl is not None else None,
        "breakeven_p": round(breakeven_p, 4) if breakeven_p is not None else None,
    }


def _poisson_binomial_pvalue(wins: int, probs: list[float]) -> float | None:
    n = len(probs)
    if n == 0 or wins <= 0:
        return None
    dp = [0.0] * (n + 1)
    dp[0] = 1.0
    for p in probs:
        for j in range(n, 0, -1):
            dp[j] = dp[j] * (1.0 - p) + dp[j - 1] * p
        dp[0] *= (1.0 - p)
    return sum(dp[wins:])


def latest_contract_status() -> dict[str, Any]:
    rows = _read_csv_rows(TRADES_CSV)
    if not rows:
        return {}
    for row in reversed(rows):
        cid = row.get("contract_id", "").strip()
        if not cid:
            continue
        return {
            "contract_id": cid,
            "event": row.get("event", ""),
            "selected_side": row.get("selected_side", "") or row.get("pred_class", ""),
            "order_status": row.get("order_status", ""),
            "pred_p_yes": row.get("pred_p_yes", ""),
            "pred_p_no": row.get("pred_p_no", ""),
            "pred_p_skip": row.get("pred_p_skip", ""),
            "correct": row.get("correct", ""),
            "pnl_ratio": row.get("pnl_ratio", ""),
            "exit_bid": row.get("exit_bid", ""),
            "close_time": row.get("close_time", ""),
            "remaining_seconds": row.get("remaining_seconds", ""),
        }
    return {}


def portfolio_history() -> list[dict[str, Any]]:
    rows = _read_csv_rows(PORTFOLIO_CSV)
    result = []
    for row in rows:
        value = row.get("portfolio_value", "")
        ts = row.get("timestamp_utc", "")
        if value and ts:
            result.append({
                "timestamp_utc": ts,
                "portfolio_value": _finite(value),
                "initial_balance": _finite(row.get("initial_balance", 0)),
            })
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/log")
def get_log() -> Response:
    n = int(request.args.get("n", 300))
    lines: list[str] = []
    if LOG_PATH.exists():
        try:
            with LOG_PATH.open(encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception:
            pass
    return jsonify({"lines": [l.rstrip() for l in lines[-n:]]})


@app.route("/stats")
def get_stats() -> Response:
    return jsonify({
        "stats": compute_stats(),
        "latest_contract": latest_contract_status(),
        "portfolio_history": portfolio_history(),
    })


@app.route("/")
def index() -> Response:
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket XRP Intra-Period Trader</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f0f14; color: #d0d0e0; font-family: 'Fira Mono', 'Consolas', monospace; font-size: 12.5px; min-height: 100vh; }
  header { background: #1a1a2e; padding: 14px 22px; display: flex; align-items: center; gap: 18px; border-bottom: 1px solid #2a2a4a; }
  header h1 { font-size: 16px; font-weight: 700; color: #a0c4ff; letter-spacing: 1px; }
  header .dot { width: 9px; height: 9px; border-radius: 50%; background: #22e08a; animation: pulse 1.8s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
  .layout { display: grid; grid-template-columns: 1fr 340px; gap: 0; height: calc(100vh - 52px); }
  .log-pane { padding: 14px 18px; overflow-y: auto; border-right: 1px solid #1e1e38; }
  .sidebar { display: flex; flex-direction: column; padding: 14px 16px; gap: 14px; overflow-y: auto; }
  pre#logbox { white-space: pre-wrap; word-break: break-all; line-height: 1.55; font-size: 11.5px; color: #c0c0d8; }
  pre#logbox .line { display: block; }
  pre#logbox .line:hover { background: rgba(160,196,255,0.06); }
  .card { background: #14142a; border: 1px solid #22224a; border-radius: 6px; padding: 12px 14px; }
  .card h2 { font-size: 10px; text-transform: uppercase; letter-spacing: 1.2px; color: #6060a0; margin-bottom: 10px; }
  .metric-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 12px; }
  .metric-item .label { font-size: 10px; color: #606090; margin-bottom: 2px; }
  .metric-item .value { font-size: 16px; font-weight: 700; }
  .metric-item .value.green { color: #22e08a; }
  .metric-item .value.red { color: #ff6060; }
  .metric-item .value.gray { color: #8080b0; }
  .metric-item .value.white { color: #d0d0e8; }
  .metric-item .value.blue { color: #a0c4ff; }
  canvas#chart { width: 100% !important; height: 120px !important; }
  .range-btns { display: flex; gap: 6px; margin-top: 6px; }
  .range-btn { flex: 1; padding: 4px 0; background: #1e1e3a; border: 1px solid #2a2a5a; border-radius: 4px;
    color: #a0a0c0; font-size: 10px; cursor: pointer; text-align: center; transition: background .15s; }
  .range-btn:hover, .range-btn.active { background: #2a2a5a; color: #a0c4ff; border-color: #5060b0; }
  .contract-box { font-size: 11px; color: #b0b0d0; line-height: 1.6; }
  .contract-box .slug { font-size: 12px; color: #a0c4ff; font-weight: 700; margin-bottom: 4px; }
  .contract-box .success { color: #22e08a; }
  .contract-box .unsuccess { color: #ff6060; }
  .contract-box .skipped { color: #8080a0; }
  .contract-box .outcome { color: #d4b896; }
  .contract-box .side { color: #c0e0c0; font-weight: 700; }
  .log-pane::-webkit-scrollbar, .sidebar::-webkit-scrollbar { width: 5px; }
  .log-pane::-webkit-scrollbar-thumb, .sidebar::-webkit-scrollbar-thumb { background: #2a2a5a; border-radius: 3px; }
  .scroll-btm-btn { position: fixed; bottom: 22px; right: 360px; background: #222248; color: #a0c4ff;
    border: 1px solid #3a3a7a; border-radius: 14px; padding: 4px 14px; font-size: 11px; cursor: pointer; }
  .scroll-btm-btn:hover { background: #2a2a5a; }
</style>
</head>
<body>
<header>
  <div class="dot" id="dot"></div>
  <h1>POLYMARKET XRP INTRA-PERIOD TRADER (T1=180s → T2=30s)</h1>
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
          <div class="label">Wins (pnl&gt;0)</div>
          <div class="value green" id="cnt-s">--</div>
        </div>
        <div class="metric-item">
          <div class="label">Losses (pnl≤0)</div>
          <div class="value red" id="cnt-u">--</div>
        </div>
        <div class="metric-item">
          <div class="label">Skipped</div>
          <div class="value gray" id="cnt-k">--</div>
        </div>
        <div class="metric-item">
          <div class="label">Win Rate</div>
          <div class="value blue" id="win-rate">--</div>
        </div>
        <div class="metric-item" style="grid-column:1/-1;">
          <div class="label">Avg PnL ratio <span style="color:#404070;font-size:9px;">(exit-based)</span></div>
          <div class="value" id="avg-pnl" style="font-size:14px;">--</div>
        </div>
        <div class="metric-item" style="grid-column:1/-1;">
          <div class="label">Breakeven p-value <span style="color:#404070;font-size:9px;">(Poisson-Binomial)</span></div>
          <div class="value" id="breakeven-p" style="font-size:14px;">--</div>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>Portfolio Value</h2>
      <canvas id="chart"></canvas>
      <div class="range-btns">
        <div class="range-btn active" data-range="4h" onclick="setRange('4h',this)">4h</div>
        <div class="range-btn" data-range="1d" onclick="setRange('1d',this)">1d</div>
        <div class="range-btn" data-range="3d" onclick="setRange('3d',this)">3d</div>
        <div class="range-btn" data-range="1w" onclick="setRange('1w',this)">1w</div>
        <div class="range-btn" data-range="all" onclick="setRange('all',this)">all</div>
      </div>
    </div>

    <div class="card">
      <h2>Latest Contract</h2>
      <div class="contract-box" id="contract-box"><span class="skipped">Waiting for data...</span></div>
    </div>

  </div>
</div>

<button class="scroll-btm-btn" onclick="scrollToBottom()" title="Scroll to bottom">↓ latest</button>

<script>
const API_INTERVAL = 3000;
let portfolioData = [];
let currentRange = '4h';
let autoScroll = true;

const logPane = document.getElementById('log-pane');
const logbox = document.getElementById('logbox');

logPane.addEventListener('scroll', () => {
  autoScroll = logPane.scrollTop + logPane.clientHeight >= logPane.scrollHeight - 30;
});

function scrollToBottom() {
  logPane.scrollTop = logPane.scrollHeight;
  autoScroll = true;
}

function colorLine(text) {
  if (text.includes('EXIT') && text.includes('pnl=+')) return {color: '#22e08a', bold: true};
  if (text.includes('EXIT') && text.includes('pnl=-')) return {color: '#ff6060', bold: true};
  if (text.includes('EXIT')) return {color: '#ffe066', bold: true};
  if (text.startsWith('ORDER') || text.includes(' | ORDER ')) return {color: '#ffe066', bold: true};
  if (text.includes('ORDER ERROR') || (text.includes('ORDER') && text.includes('error'))) return {color: '#ff8060', bold: true};
  if (text.includes('FEATURES ')) return {color: '#c0a0ff'};
  if (text.includes('SKIP') || text.includes('skip')) return {color: '#8080a0'};
  if (text.includes('CONTRACT ')) return {color: '#a0c4ff'};
  if (text.includes('MODEL')) return {color: '#d4b896'};
  if (text.includes('STOP')) return {color: '#ff6060'};
  if (text.includes('BALANCE')) return {color: '#c0e0c0'};
  return {};
}

function updateLog(lines) {
  const frag = document.createDocumentFragment();
  lines.forEach(line => {
    const span = document.createElement('span');
    span.className = 'line';
    span.textContent = line;
    const style = colorLine(line);
    if (style.color) span.style.color = style.color;
    if (style.bold) span.style.fontWeight = '700';
    frag.appendChild(span);
  });
  logbox.innerHTML = '';
  logbox.appendChild(frag);
  if (autoScroll) scrollToBottom();
}

function updateStats(data) {
  const s = data.stats || {};
  const el = id => document.getElementById(id);
  el('cnt-s').textContent = s.wins ?? '--';
  el('cnt-u').textContent = s.losses ?? '--';
  el('cnt-k').textContent = s.skipped ?? '--';
  el('win-rate').textContent = s.win_rate != null ? s.win_rate.toFixed(1) + '%' : '--';
  el('header-status').textContent = `W:${s.wins||0} L:${s.losses||0} K:${s.skipped||0}`;

  const avgEl = el('avg-pnl');
  if (s.avg_pnl_ratio != null) {
    avgEl.textContent = (s.avg_pnl_ratio >= 0 ? '+' : '') + (s.avg_pnl_ratio * 100).toFixed(2) + '%';
    avgEl.style.color = s.avg_pnl_ratio >= 0 ? '#22e08a' : '#ff6060';
  } else {
    avgEl.textContent = '--';
    avgEl.style.color = '#8080b0';
  }

  const bpEl = el('breakeven-p');
  if (s.breakeven_p != null) {
    bpEl.textContent = s.breakeven_p < 0.001 ? s.breakeven_p.toExponential(2) : s.breakeven_p.toFixed(4);
    bpEl.style.color = s.breakeven_p <= 0.01 ? '#22e08a' : s.breakeven_p <= 0.05 ? '#ffe066' : '#8080b0';
  } else {
    bpEl.textContent = '--';
    bpEl.style.color = '#8080b0';
  }

  const lc = data.latest_contract || {};
  const box = document.getElementById('contract-box');
  if (!lc.contract_id) { box.innerHTML = '<span class="skipped">Waiting for data...</span>'; return; }
  const status = lc.order_status || '';
  const side = lc.selected_side || '--';
  const correct = lc.correct;
  const pnl = lc.pnl_ratio ? parseFloat(lc.pnl_ratio) : null;
  const cls = correct === '1' ? 'success' : correct === '0' ? 'unsuccess' : 'skipped';
  const resultText = correct === '1' ? 'WIN' : correct === '0' ? 'LOSS' : (status === 'skip' ? 'SKIP' : status.toUpperCase());
  const pnlStr = pnl !== null ? (pnl >= 0 ? '+' : '') + (pnl * 100).toFixed(2) + '%' : '';
  box.innerHTML = `
    <div class="slug">${lc.contract_id}</div>
    <div>Side: <span class="side">${side}</span></div>
    <div>Status: <span class="${cls}">${resultText}</span>${pnlStr ? ' <span style="font-size:10px;color:#a0a0c0;">pnl=' + pnlStr + '</span>' : ''}</div>
    ${lc.exit_bid ? '<div style="font-size:10px;color:#606090;">exit_bid=' + parseFloat(lc.exit_bid).toFixed(3) + '</div>' : ''}
    ${lc.pred_p_yes ? '<div style="font-size:10px;color:#606090;">P(Yes)=' + parseFloat(lc.pred_p_yes).toFixed(3) + ' P(No)=' + parseFloat(lc.pred_p_no||0).toFixed(3) + ' P(Skip)=' + parseFloat(lc.pred_p_skip||0).toFixed(3) + '</div>' : ''}
    ${lc.close_time ? '<div style="font-size:10px;color:#505080;">close ' + lc.close_time.slice(11,19) + 'Z</div>' : ''}
  `;
}

function rangeMs(r) {
  const map = { '4h': 4*3600*1000, '1d': 86400*1000, '3d': 3*86400*1000, '1w': 7*86400*1000, 'all': Infinity };
  return map[r] || Infinity;
}

function setRange(r, btn) {
  currentRange = r;
  document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  drawChart();
}

function updatePortfolio(history) {
  portfolioData = history;
  drawChart();
}

function drawChart() {
  const canvas = document.getElementById('chart');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.offsetWidth;
  const H = canvas.offsetHeight;
  canvas.width = W * window.devicePixelRatio;
  canvas.height = H * window.devicePixelRatio;
  ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
  ctx.clearRect(0, 0, W, H);
  const limit = rangeMs(currentRange);
  const now = Date.now();
  const filtered = portfolioData.filter(p => {
    try { return now - new Date(p.timestamp_utc).getTime() <= limit; } catch { return false; }
  });
  if (filtered.length < 2) {
    ctx.fillStyle = '#606090'; ctx.font = '11px monospace'; ctx.textAlign = 'center';
    ctx.fillText('No data', W/2, H/2); return;
  }
  const vals = filtered.map(p => p.portfolio_value);
  const minV = Math.min(...vals); const maxV = Math.max(...vals);
  const range = maxV - minV || 0.01;
  const pad = { l: 40, r: 10, t: 10, b: 24 };
  const gW = W - pad.l - pad.r; const gH = H - pad.t - pad.b;
  ctx.strokeStyle = '#1e1e3a'; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + gH * (1 - i / 4);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + gW, y); ctx.stroke();
    ctx.fillStyle = '#505080'; ctx.font = '9px monospace'; ctx.textAlign = 'right';
    ctx.fillText('$' + (minV + range * i / 4).toFixed(2), pad.l - 3, y + 3);
  }
  const initV = filtered[0].initial_balance || filtered[0].portfolio_value;
  ctx.setLineDash([3, 4]); ctx.strokeStyle = '#404070'; ctx.lineWidth = 1;
  const baselineY = pad.t + gH * (1 - (initV - minV) / range);
  ctx.beginPath(); ctx.moveTo(pad.l, baselineY); ctx.lineTo(pad.l + gW, baselineY); ctx.stroke();
  ctx.setLineDash([]);
  ctx.beginPath();
  filtered.forEach((p, i) => {
    const x = pad.l + gW * i / (filtered.length - 1);
    const y = pad.t + gH * (1 - (p.portfolio_value - minV) / range);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  const latestVal = filtered[filtered.length - 1].portfolio_value;
  ctx.strokeStyle = latestVal >= initV ? '#22e08a' : '#ff6060';
  ctx.lineWidth = 1.5; ctx.stroke();
  ctx.fillStyle = '#606090'; ctx.font = '9px monospace';
  ctx.textAlign = 'left';
  ctx.fillText(new Date(filtered[0].timestamp_utc).toISOString().slice(11,16)+'Z', pad.l, H-5);
  ctx.textAlign = 'right';
  ctx.fillText(new Date(filtered[filtered.length-1].timestamp_utc).toISOString().slice(11,16)+'Z', pad.l+gW, H-5);
}

async function refreshAll() {
  try {
    const [logResp, statsResp] = await Promise.all([fetch('/log?n=300'), fetch('/stats')]);
    const logData = await logResp.json();
    const statsData = await statsResp.json();
    updateLog(logData.lines || []);
    updateStats(statsData);
    updatePortfolio(statsData.portfolio_history || []);
    document.getElementById('dot').style.background = '#22e08a';
  } catch (e) {
    document.getElementById('dot').style.background = '#ff6060';
  }
}

refreshAll();
setInterval(refreshAll, API_INTERVAL);
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--host", default="0.0.0.0")
    a = p.parse_args()
    print(f"Starting XRP intra-period trader dashboard at http://{a.host}:{a.port}")
    app.run(host=a.host, port=a.port, debug=False, use_reloader=False)
