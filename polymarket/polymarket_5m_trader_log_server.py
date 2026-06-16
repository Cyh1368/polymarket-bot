#!/usr/bin/env python3
"""Flask dashboard for polymarket_5m_trader — serves at 0.0.0.0:8099."""
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
LOG_PATH = Path(os.getenv("POLYMARKET_TRADER_LOG", str(APP_DIR / "polymarket_5m_trader.log")))
TRADES_CSV = Path(os.getenv("POLYMARKET_TRADER_TRADES_CSV", str(APP_DIR / "polymarket_5m_trader_trades.csv")))
PORTFOLIO_CSV = Path(os.getenv("POLYMARKET_TRADER_PORTFOLIO_CSV", str(APP_DIR / "polymarket_5m_trader_portfolio.csv")))
CONTRACT_VALUE = float(os.getenv("POLYMARKET_CONTRACT_VALUE", "2.0"))

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


def _poisson_binomial_pvalue(wins: int, probs: list[float]) -> float | None:
    """P(X >= wins) where X ~ PoissonBinomial(probs). Exact DP, O(n^2)."""
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


def compute_stats() -> dict[str, Any]:
    rows = _read_csv_rows(TRADES_CSV)
    outcome_rows = [r for r in rows if r.get("event") == "outcome"]
    skipped = sum(1 for r in rows if r.get("event") == "decision" and r.get("order_status") == "skip")

    decided_rows = [r for r in outcome_rows if str(r.get("correct", "")).strip() in ("0", "1")]

    def _side_stats(side: str) -> tuple[int, int]:
        sw = sum(1 for r in decided_rows if r.get("selected_side") == side and r.get("correct") == "1")
        sl = sum(1 for r in decided_rows if r.get("selected_side") == side and r.get("correct") == "0")
        return sw, sl

    yes_w, yes_l = _side_stats("YES")
    no_w,  no_l  = _side_stats("NO")
    wins   = yes_w + no_w
    losses = yes_l + no_l
    decided = wins + losses

    def _wr(w, n): return round(w / n * 100, 1) if n else None

    # Dollar P&L: win → filled_size × (1 − fill_price); loss → −filled_size × fill_price
    dollar_pnl = 0.0
    for r in decided_rows:
        fp = _finite(r.get("fill_price") or r.get("selected_ask"))
        fs = _finite(r.get("filled_size") or r.get("contracts") or 1)
        if r.get("correct") == "1":
            dollar_pnl += fs * (1.0 - fp)
        else:
            dollar_pnl -= fs * fp

    n_avail = decided + skipped
    ev_avail = dollar_pnl / (CONTRACT_VALUE * n_avail) if n_avail else None

    # Breakeven p-value (Poisson-Binomial): P(wins ≥ observed | null = buy at ask price)
    ask_probs = [_finite(r.get("selected_ask")) for r in decided_rows]
    ask_probs = [p for p in ask_probs if 0.0 < p < 1.0]
    breakeven_p = _poisson_binomial_pvalue(wins, ask_probs) if len(ask_probs) == decided and decided > 0 else None

    # Regime breakdown — count decisions by regime (from regime column added 2026-06-16)
    regime_counts: dict[str, dict[str, int]] = {}
    for r in rows:
        if r.get("event") != "decision":
            continue
        reg = r.get("regime", "") or "?"
        if reg not in regime_counts:
            regime_counts[reg] = {"decided": 0, "skipped": 0}
        if r.get("order_status") == "skip":
            regime_counts[reg]["skipped"] += 1
        elif str(r.get("correct", "")).strip() in ("0", "1"):
            regime_counts[reg]["decided"] += 1

    # Latest regime from most recent decision row that has it
    current_regime = None
    current_btc_4h_ret = None
    for r in reversed(rows):
        if r.get("event") == "decision" and r.get("regime"):
            current_regime = r.get("regime")
            raw_ret = r.get("btc_4h_ret", "")
            try:
                current_btc_4h_ret = round(float(raw_ret) * 100, 3) if raw_ret else None
            except (TypeError, ValueError):
                current_btc_4h_ret = None
            break

    return {
        "total_decided": decided,
        "wins": wins,
        "losses": losses,
        "skipped": skipped,
        "win_rate":     _wr(wins,  decided),
        "yes_wins":     yes_w,
        "yes_losses":   yes_l,
        "yes_win_rate": _wr(yes_w, yes_w + yes_l),
        "no_wins":      no_w,
        "no_losses":    no_l,
        "no_win_rate":  _wr(no_w,  no_w + no_l),
        "dollar_pnl":   round(dollar_pnl, 2),
        "ev_avail":     round(ev_avail, 4) if ev_avail is not None else None,
        "breakeven_p":  round(breakeven_p, 4) if breakeven_p is not None else None,
        "current_regime":    current_regime,
        "btc_4h_ret_pct":    current_btc_4h_ret,
        "regime_counts":     regime_counts,
    }


def latest_contract_status() -> dict[str, Any]:
    rows = _read_csv_rows(TRADES_CSV)
    if not rows:
        return {}
    # find most-recent contract entry
    for row in reversed(rows):
        cid = row.get("contract_id", "").strip()
        if not cid:
            continue
        event = row.get("event", "").strip()
        side = row.get("selected_side", "") or row.get("pred_class", "")
        status = row.get("order_status", "")
        pred_yes = row.get("pred_p_yes", "")
        pred_no = row.get("pred_p_no", "")
        pred_skip = row.get("pred_p_skip", "")
        correct = row.get("correct", "")
        actual = row.get("actual_side", "")
        return {
            "contract_id": cid,
            "event": event,
            "selected_side": side,
            "order_status": status,
            "pred_p_yes": pred_yes,
            "pred_p_no": pred_no,
            "pred_p_skip": pred_skip,
            "correct": correct,
            "actual_side": actual,
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
<title>Polymarket 5m Trader</title>
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
  <h1>POLYMARKET 5m TRADER — BTC/USD</h1>
  <span id="header-status" style="margin-left:auto;font-size:11px;color:#606090;">loading...</span>
</header>

<div class="layout">
  <!-- Log pane -->
  <div class="log-pane" id="log-pane">
    <pre id="logbox"></pre>
  </div>

  <!-- Sidebar -->
  <div class="sidebar">

    <!-- Counts / win rate -->
    <div class="card">
      <h2>Session Stats</h2>
      <div class="metric-grid">
        <div class="metric-item">
          <div class="label">W / L / Skip</div>
          <div class="value white" id="cnt-wls">-- / -- / --</div>
        </div>
        <div class="metric-item">
          <div class="label">P&amp;L</div>
          <div class="value" id="dollar-pnl">--</div>
        </div>
        <div class="metric-item">
          <div class="label">Win Rate (all)</div>
          <div class="value blue" id="win-rate">--</div>
        </div>
        <div class="metric-item">
          <div class="label">EV / Available</div>
          <div class="value" id="ev-avail">--</div>
        </div>
        <div class="metric-item">
          <div class="label">Win Rate YES <span style="color:#404070;font-size:9px;">exp 61%</span></div>
          <div class="value blue" id="yes-wr">--</div>
        </div>
        <div class="metric-item">
          <div class="label">Win Rate NO <span style="color:#404070;font-size:9px;">exp 59%</span></div>
          <div class="value blue" id="no-wr">--</div>
        </div>
        <div class="metric-item">
          <div class="label">YES W / L</div>
          <div class="value gray" id="yes-wl">--</div>
        </div>
        <div class="metric-item">
          <div class="label">NO W / L</div>
          <div class="value gray" id="no-wl">--</div>
        </div>
        <div class="metric-item" style="grid-column:1/-1;">
          <div class="label">Breakeven p-value <span style="color:#404070;font-size:9px;">(wins ≥ avg cost; Poisson-Binomial)</span></div>
          <div class="value" id="breakeven-p" style="font-size:14px;">--</div>
        </div>
      </div>
    </div>

    <!-- Portfolio chart -->
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

    <!-- Regime -->
    <div class="card">
      <h2>BTC Regime</h2>
      <div class="metric-grid">
        <div class="metric-item">
          <div class="label">Current Regime</div>
          <div class="value" id="regime-label" style="font-size:20px;">--</div>
        </div>
        <div class="metric-item">
          <div class="label">BTC 4h Return</div>
          <div class="value" id="btc-4h-ret">--</div>
        </div>
        <div class="metric-item" style="grid-column:1/-1;font-size:10px;color:#606090;">
          UP=skip &nbsp;|&nbsp; FLAT/DOWN=trade &nbsp;|&nbsp; threshold ±0.3%
        </div>
      </div>
    </div>

    <!-- Latest contract -->
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
  if (text.includes('OUTCOME') && text.includes('result=successful')) return {color: '#22e08a'};
  if (text.includes('OUTCOME') && text.includes('result=unsuccessful')) return {color: '#ff6060'};
  if (text.includes('OUTCOME') && text.includes('result=skipped')) return {color: '#8080a0'};
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

  el('cnt-wls').textContent = `${s.wins??'--'} / ${s.losses??'--'} / ${s.skipped??'--'}`;
  el('win-rate').textContent = s.win_rate != null ? s.win_rate.toFixed(1) + '%' : '--';

  const pnlEl = el('dollar-pnl');
  if (s.dollar_pnl != null) {
    pnlEl.textContent = (s.dollar_pnl >= 0 ? '+' : '') + '$' + s.dollar_pnl.toFixed(2);
    pnlEl.style.color = s.dollar_pnl >= 0 ? '#22e08a' : '#ff6060';
  } else { pnlEl.textContent = '--'; pnlEl.style.color = '#8080b0'; }

  const evEl = el('ev-avail');
  if (s.ev_avail != null) {
    evEl.textContent = (s.ev_avail >= 0 ? '+' : '') + (s.ev_avail * 100).toFixed(2) + '%';
    evEl.style.color = s.ev_avail >= 0.025 ? '#22e08a' : s.ev_avail >= 0 ? '#ffe066' : '#ff6060';
  } else { evEl.textContent = '--'; evEl.style.color = '#8080b0'; }

  const wrColor = (wr, exp) => wr == null ? '#8080b0' : wr >= exp ? '#22e08a' : wr >= exp - 5 ? '#ffe066' : '#ff6060';
  el('yes-wr').textContent = s.yes_win_rate != null ? s.yes_win_rate.toFixed(1) + '%' : '--';
  el('yes-wr').style.color = wrColor(s.yes_win_rate, 61);
  el('no-wr').textContent  = s.no_win_rate  != null ? s.no_win_rate.toFixed(1)  + '%' : '--';
  el('no-wr').style.color  = wrColor(s.no_win_rate, 59);

  // Regime card
  const regEl = el('regime-label');
  const retEl = el('btc-4h-ret');
  if (s.current_regime) {
    regEl.textContent = s.current_regime;
    regEl.style.color = s.current_regime === 'UP' ? '#ff6060' : s.current_regime === 'DOWN' ? '#a0c4ff' : '#22e08a';
  } else { regEl.textContent = '--'; regEl.style.color = '#8080b0'; }
  if (s.btc_4h_ret_pct != null) {
    retEl.textContent = (s.btc_4h_ret_pct >= 0 ? '+' : '') + s.btc_4h_ret_pct.toFixed(3) + '%';
    retEl.style.color = s.btc_4h_ret_pct > 0.3 ? '#ff6060' : s.btc_4h_ret_pct < -0.3 ? '#a0c4ff' : '#22e08a';
  } else { retEl.textContent = '--'; retEl.style.color = '#8080b0'; }
  el('yes-wl').textContent = `${s.yes_wins??'--'} / ${s.yes_losses??'--'}`;
  el('no-wl').textContent  = `${s.no_wins??'--'} / ${s.no_losses??'--'}`;

  el('header-status').textContent = `W:${s.wins||0} L:${s.losses||0} K:${s.skipped||0} ev:${s.ev_avail != null ? (s.ev_avail*100).toFixed(1)+'%' : '--'}`;

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
  const cls = correct === '1' ? 'success' : correct === '0' ? 'unsuccess' : 'skipped';
  const resultText = correct === '1' ? 'WIN' : correct === '0' ? 'LOSS' : (status === 'skip' ? 'SKIP' : status.toUpperCase());
  box.innerHTML = `
    <div class="slug">${lc.contract_id}</div>
    <div>Side: <span class="side">${side}</span></div>
    <div>Status: <span class="${cls}">${resultText}</span></div>
    ${lc.actual_side ? '<div>Actual: <span class="outcome">' + lc.actual_side + '</span></div>' : ''}
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
    try { return now - new Date(p.timestamp_utc).getTime() <= limit; }
    catch { return false; }
  });
  if (filtered.length < 2) {
    ctx.fillStyle = '#606090';
    ctx.font = '11px monospace';
    ctx.textAlign = 'center';
    ctx.fillText('No data', W/2, H/2);
    return;
  }

  const vals = filtered.map(p => p.portfolio_value);
  const minV = Math.min(...vals);
  const maxV = Math.max(...vals);
  const range = maxV - minV || 0.01;
  const pad = { l: 40, r: 10, t: 10, b: 24 };
  const gW = W - pad.l - pad.r;
  const gH = H - pad.t - pad.b;

  ctx.strokeStyle = '#1e1e3a';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + gH * (1 - i / 4);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + gW, y); ctx.stroke();
    const v = minV + range * i / 4;
    ctx.fillStyle = '#505080';
    ctx.font = '9px monospace';
    ctx.textAlign = 'right';
    ctx.fillText('$' + v.toFixed(2), pad.l - 3, y + 3);
  }

  const initV = filtered[0].initial_balance || filtered[0].portfolio_value;
  ctx.setLineDash([3, 4]);
  ctx.strokeStyle = '#404070';
  ctx.lineWidth = 1;
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
  ctx.lineWidth = 1.5;
  ctx.stroke();

  ctx.fillStyle = '#606090';
  ctx.font = '9px monospace';
  ctx.textAlign = 'left';
  const t0 = new Date(filtered[0].timestamp_utc);
  const t1 = new Date(filtered[filtered.length - 1].timestamp_utc);
  ctx.fillText(t0.toISOString().slice(11, 16) + 'Z', pad.l, H - 5);
  ctx.textAlign = 'right';
  ctx.fillText(t1.toISOString().slice(11, 16) + 'Z', pad.l + gW, H - 5);
}

async function refreshAll() {
  try {
    const [logResp, statsResp] = await Promise.all([
      fetch('/log?n=300'),
      fetch('/stats'),
    ]);
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
    print(f"Starting Polymarket 5m trader dashboard at http://{a.host}:{a.port}")
    app.run(host=a.host, port=a.port, debug=False, use_reloader=False)
