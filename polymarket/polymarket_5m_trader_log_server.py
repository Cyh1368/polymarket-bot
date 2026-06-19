#!/usr/bin/env python3
"""Flask dashboard for polymarket_5m_trader — serves at 0.0.0.0:8099."""
from __future__ import annotations

import csv
import json
import math
import os
import time
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


def detect_session_config() -> dict[str, Any]:
    """Parse the last START line from the log for mode + all key config values."""
    config: dict[str, Any] = {"mode": "DRY"}
    if not LOG_PATH.exists():
        return config
    import re
    try:
        with LOG_PATH.open(encoding="utf-8", errors="replace") as f:
            for line in reversed(f.readlines()):
                if "START" in line and "polymarket_5m_trader" in line:
                    config["mode"] = "LIVE" if "LIVE TRADING" in line else "DRY"
                    for key in ("model", "filters", "skip_bonus", "entry_seconds",
                                "contract_value", "stop_loss", "tolerance"):
                        m = re.search(rf"{key}=(\S+)", line)
                        if m:
                            config[key] = m.group(1)
                    break
    except Exception:
        pass
    return config


def detect_mode() -> str:
    return detect_session_config()["mode"]


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

    # Dollar P&L: use dollar_pnl column (written by trader with fee) when present,
    # otherwise fall back to gross formula for backwards compat with old CSVs.
    dollar_pnl = 0.0
    for r in decided_rows:
        raw = r.get("dollar_pnl", "").strip()
        if raw:
            dollar_pnl += _finite(raw)
        else:
            fp = _finite(r.get("fill_price") or r.get("selected_ask"))
            fs = _finite(r.get("filled_size") or r.get("contracts") or 1)
            fee = math.ceil(0.07 * fp * (1.0 - fp) * 100) / 100
            gross = fs * (1.0 - fp) if r.get("correct") == "1" else -(fs * fp)
            dollar_pnl += gross - fs * fee

    n_avail = decided + skipped
    ev_avail = dollar_pnl / (CONTRACT_VALUE * n_avail) if n_avail else None

    # Breakeven p-value (Poisson-Binomial): P(wins ≥ observed | null = buy at ask price)
    ask_probs = [_finite(r.get("selected_ask")) for r in decided_rows]
    ask_probs = [p for p in ask_probs if 0.0 < p < 1.0]
    breakeven_p = _poisson_binomial_pvalue(wins, ask_probs) if len(ask_probs) == decided and decided > 0 else None

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
        "mode":         detect_mode(),
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


def ev_history() -> list[dict[str, Any]]:
    """Running EV/trade (mean dollar P&L) with 95% CI at each resolved trade."""
    rows = _read_csv_rows(TRADES_CSV)
    outcome_rows = [
        r for r in rows
        if r.get("event") == "outcome" and str(r.get("correct", "")).strip() in ("0", "1")
    ]
    result = []
    cumsum = 0.0
    cumsum2 = 0.0
    for i, r in enumerate(outcome_rows, 1):
        ts = r.get("timestamp_utc", "")
        if not ts:
            continue
        raw = r.get("dollar_pnl", "").strip()
        if raw:
            pnl = _finite(raw)
        else:
            fp = _finite(r.get("fill_price") or r.get("selected_ask"))
            fs = _finite(r.get("filled_size") or r.get("contracts") or 1)
            fee = math.ceil(0.07 * fp * (1.0 - fp) * 100) / 100
            pnl = (fs * (1.0 - fp) - fs * fee) if r.get("correct") == "1" else -(fs * fp + fs * fee)
        cumsum  += pnl
        cumsum2 += pnl * pnl
        mean = cumsum / i
        if i > 1:
            var = max((cumsum2 - cumsum * cumsum / i) / (i - 1), 0.0)
            se = math.sqrt(var / i)
        else:
            se = 0.0
        result.append({
            "timestamp_utc": ts,
            "n": i,
            "ev_mean": round(mean, 4),
            "ci_lo":   round(mean - 1.96 * se, 4),
            "ci_hi":   round(mean + 1.96 * se, 4),
        })
    return result


def up_rate_24h() -> dict[str, Any]:
    """Compute Up% from settled outcome rows in the last 24 h."""
    rows = _read_csv_rows(TRADES_CSV)
    cutoff = time.time() - 86400.0
    ups, total = 0, 0
    for r in rows:
        if r.get("event") != "outcome":
            continue
        actual = r.get("actual_side", "").strip()
        if actual not in ("Up", "Down"):
            continue
        try:
            from datetime import datetime, timezone
            t = datetime.fromisoformat(r.get("timestamp_utc", "").replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if t < cutoff:
            continue
        total += 1
        if actual == "Up":
            ups += 1
    return {
        "up_rate_24h": round(ups / total, 4) if total else None,
        "n_settled_24h": total,
    }


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
    session_config = detect_session_config()
    stats = compute_stats()
    stats["mode"] = session_config["mode"]
    return jsonify({
        "stats": stats,
        "session_config": session_config,
        "latest_contract": latest_contract_status(),
        "portfolio_history": portfolio_history(),
        "ev_history": ev_history(),
        "up_rate_24h": up_rate_24h(),
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
  #mode-badge { font-size: 12px; font-weight: 700; padding: 3px 10px; border-radius: 4px; letter-spacing: 1px; }
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
  canvas#ev-chart { width: 100% !important; height: 130px !important; }
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
  <span id="mode-badge">DRY TESTING</span>
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

    <!-- Up% 24h regime card -->
    <div class="card">
      <h2>BTC Regime — Up% in past 24h</h2>
      <div class="metric-grid">
        <div class="metric-item">
          <div class="label">Up% <span style="color:#404070;font-size:9px;">settled contracts</span></div>
          <div class="value" id="up-rate-24h">--</div>
        </div>
        <div class="metric-item">
          <div class="label">Settled (24h)</div>
          <div class="value gray" id="n-settled-24h">--</div>
        </div>
        <div class="metric-item" style="grid-column:1/-1;">
          <div class="label" style="font-size:9px;color:#404070;">Backtest mean: 50.0% · balanced ±4pp/day · warning if >55% or <45%</div>
        </div>
      </div>
    </div>

    <!-- EV/trade chart -->
    <div class="card">
      <h2>EV / Trade vs. Time <span style="color:#404070;font-size:9px;">running mean ± 95% CI</span></h2>
      <canvas id="ev-chart"></canvas>
    </div>

    <!-- Mode -->
    <div class="card">
      <h2>Trading Mode</h2>
      <div class="metric-grid">
        <div class="metric-item" style="grid-column:1/-1;">
          <div class="value" id="mode-label" style="font-size:22px;">--</div>
        </div>
        <div class="metric-item" style="grid-column:1/-1;font-size:10px;color:#606090;line-height:1.7;" id="mode-desc">
          —
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
let evData = [];
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
  const cfg = data.session_config || {};
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

  // Mode card + header badge
  const mode = s.mode || 'DRY';
  const isLive = mode === 'LIVE';
  const modeColor = isLive ? '#ff6060' : '#ffe066';
  el('mode-label').textContent = isLive ? 'LIVE TRADING' : 'DRY TESTING';
  el('mode-label').style.color = modeColor;
  const badge = el('mode-badge');
  badge.textContent = isLive ? '*** LIVE ***' : '--- DRY ---';
  badge.style.background = isLive ? 'rgba(255,60,60,0.18)' : 'rgba(255,220,60,0.12)';
  badge.style.color = modeColor;
  badge.style.border = `1px solid ${modeColor}44`;

  el('yes-wl').textContent = `${s.yes_wins??'--'} / ${s.yes_losses??'--'}`;
  el('no-wl').textContent  = `${s.no_wins??'--'} / ${s.no_losses??'--'}`;

  // Mode description — built from live session config, never hardcoded
  const descLines = [];
  if (cfg.model)          descLines.push(`model: ${cfg.model}`);
  if (cfg.filters)        descLines.push(`filters: ${cfg.filters}`);
  if (cfg.skip_bonus)     descLines.push(`skip_bonus: ${cfg.skip_bonus}`);
  if (cfg.entry_seconds)  descLines.push(`entry: T=${cfg.entry_seconds}s`);
  if (cfg.contract_value) descLines.push(`contract: ${cfg.contract_value}`);
  if (cfg.stop_loss)      descLines.push(`stop_loss: ${cfg.stop_loss}`);
  el('mode-desc').innerHTML = descLines.join('<br>') || '—';

  el('header-status').textContent = `[${mode}] W:${s.wins||0} L:${s.losses||0} K:${s.skipped||0} ev:${s.ev_avail != null ? (s.ev_avail*100).toFixed(1)+'%' : '--'}`;

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

function updateUpRate(data) {
  const el = id => document.getElementById(id);
  const rate = data.up_rate_24h;
  const n = data.n_settled_24h;
  el('n-settled-24h').textContent = n != null ? n : '--';
  const rateEl = el('up-rate-24h');
  if (rate == null) { rateEl.textContent = '--'; rateEl.style.color = '#8080b0'; return; }
  rateEl.textContent = (rate * 100).toFixed(1) + '%';
  rateEl.style.color = (rate >= 0.45 && rate <= 0.55) ? '#22e08a'
                     : (rate >= 0.40 && rate <= 0.60) ? '#ffe066'
                     : '#ff6060';
}

function updateEvHistory(history) {
  evData = history;
  drawEvChart();
}

function drawEvChart() {
  const canvas = document.getElementById('ev-chart');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.offsetWidth;
  const H = canvas.offsetHeight;
  canvas.width = W * window.devicePixelRatio;
  canvas.height = H * window.devicePixelRatio;
  ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
  ctx.clearRect(0, 0, W, H);

  if (evData.length < 2) {
    ctx.fillStyle = '#606090';
    ctx.font = '11px monospace';
    ctx.textAlign = 'center';
    ctx.fillText(evData.length === 0 ? 'No trades yet' : 'Need ≥2 trades', W/2, H/2);
    return;
  }

  const pad = { l: 44, r: 10, t: 10, b: 24 };
  const gW = W - pad.l - pad.r;
  const gH = H - pad.t - pad.b;

  const allY = evData.flatMap(d => [d.ci_lo, d.ci_hi]);
  const minY = Math.min(0, ...allY);
  const maxY = Math.max(0, ...allY);
  const rangeY = maxY - minY || 0.01;

  function toX(i) { return pad.l + gW * i / (evData.length - 1); }
  function toY(v) { return pad.t + gH * (1 - (v - minY) / rangeY); }

  // Grid lines
  ctx.strokeStyle = '#1e1e3a';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 3; i++) {
    const v = minY + rangeY * i / 3;
    const y = toY(v);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + gW, y); ctx.stroke();
    ctx.fillStyle = '#505080';
    ctx.font = '9px monospace';
    ctx.textAlign = 'right';
    ctx.fillText((v >= 0 ? '+' : '') + '$' + v.toFixed(2), pad.l - 3, y + 3);
  }

  // Zero line
  const zeroY = toY(0);
  ctx.setLineDash([3, 4]);
  ctx.strokeStyle = '#404070';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad.l, zeroY); ctx.lineTo(pad.l + gW, zeroY); ctx.stroke();
  ctx.setLineDash([]);

  // CI band
  ctx.beginPath();
  evData.forEach((d, i) => { i === 0 ? ctx.moveTo(toX(i), toY(d.ci_hi)) : ctx.lineTo(toX(i), toY(d.ci_hi)); });
  for (let i = evData.length - 1; i >= 0; i--) ctx.lineTo(toX(i), toY(evData[i].ci_lo));
  ctx.closePath();
  ctx.fillStyle = 'rgba(160,196,255,0.12)';
  ctx.fill();

  // Mean line
  const lastMean = evData[evData.length - 1].ev_mean;
  const lineColor = lastMean >= 0.025 ? '#22e08a' : lastMean >= 0 ? '#ffe066' : '#ff6060';
  ctx.beginPath();
  evData.forEach((d, i) => { i === 0 ? ctx.moveTo(toX(i), toY(d.ev_mean)) : ctx.lineTo(toX(i), toY(d.ev_mean)); });
  ctx.strokeStyle = lineColor;
  ctx.lineWidth = 1.8;
  ctx.stroke();

  // Time labels
  ctx.fillStyle = '#606090';
  ctx.font = '9px monospace';
  ctx.textAlign = 'left';
  ctx.fillText(new Date(evData[0].timestamp_utc).toISOString().slice(11, 16) + 'Z', pad.l, H - 5);
  ctx.textAlign = 'right';
  const lastTs = new Date(evData[evData.length - 1].timestamp_utc).toISOString().slice(11, 16) + 'Z';
  ctx.fillText(lastTs, pad.l + gW, H - 5);

  // Annotate final value
  ctx.textAlign = 'right';
  ctx.font = '9px monospace';
  ctx.fillStyle = lineColor;
  const last = evData[evData.length - 1];
  ctx.fillText(`n=${last.n} | ${(last.ev_mean >= 0 ? '+' : '')}$${last.ev_mean.toFixed(3)}/trade`, pad.l + gW, pad.t + 10);
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
    updateEvHistory(statsData.ev_history || []);
    updateUpRate(statsData.up_rate_24h || {});
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
